"""Intelligent duplicate detection for the case-management domain.

Spec (`Intelligent Duplicate Detection.txt`):

  Implement enterprise duplicate detection.
  Before a case is created
  compare Topic, Description, Category, Location, Recent submissions
  using PostgreSQL Full Text Search.
  If a similar case exists
  display Support Existing Case
  or
  Create New Case
  Never force users into either choice.
  Store supporters separately.

DESIGN

This module is a service-layer facade over a single SQL query that
combines two Postgres signals:

  1. `ts_rank(cases.search_vector, plainto_tsquery(...))` — the
     existing GIN index `ix_cases_search_vector` (installed by
     migration c2b3c4d5e6f7) handles the `@@` predicate.
  2. `similarity(title || ' ' || description, ...)` — pg_trgm's
     trigram similarity (installed by migration c5d6e7f8g9h0).
     Catches typos like "healt" vs "health".

Both signals are combined into a composite score, plus bonuses for
exact category_id and district_id matches. Pure-functional scoring
keeps unit tests DB-free (mirrors the `_rank` / `_score_candidate`
pattern in `routing.py`).

The service NEVER imports `User` — only reads `cases.*` columns. This
preserves the anonymity invariant: an MP viewer of the duplicate-check
dialog cannot learn the identity of the original reporter.

ANONYMITY INVARIANT (cross-check)

  The wire shape returned to the citizen-filing dialog and the
  MP-viewing MP inbox is IDENTICAL: only `case_id`, `case_number`,
  `category_id`, `title`, `description_snippet`, `district_id`,
  `constituency`, `similarity_score`, `support_count`, `submitted_at`.
  No `reporter_user_id`, `first_name`, `last_name`, `email`, `phone`,
  `photo_url`. The Pydantic schema (`CaseDuplicateCandidateOut`) is the
  enforcement point; the unit test
  `duplicate_candidate_schema_has_no_pii_fields` asserts the invariant.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_LOGGER = logging.getLogger("CIVCON.case_duplicates")


# ---------------------------------------------------------------------------
# Constants — single source of truth, exposed for tests.
# ---------------------------------------------------------------------------

# Status values considered "terminal" — duplicates of these are NOT
# surfaced because the case is closed for business.
NON_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "submitted", "received", "assigned", "under_review",
    "information_requested", "citizen_responded", "in_progress",
    "resolved",  # resolved is reopen-able per the workflow, so still candidate-worthy
})

TERMINAL_STATUSES: frozenset[str] = frozenset({
    "withdrawn", "rejected", "closed",
})

# How recent a candidate must be to surface. Spec asks for "recent
# submissions" — 30 days is the default window. Exposed as a kwarg so
# tests can shrink it to zero.
DEFAULT_WINDOW_DAYS: int = 30

# Max candidates returned. Five is the UX cap — anything more
# overwhelms the dialog. Exposed as a kwarg for tests.
DEFAULT_LIMIT: int = 5

# Trigram similarity threshold for the OR-clause in the WHERE filter.
# Below this, the candidate is dropped. Tuned conservatively — the
# ranker will re-order by composite score; this is the gate, not the
# ordering signal.
TRGM_THRESHOLD: float = 0.2

# Composite-score weights.
WEIGHT_FTS_RANK: float = 0.6
WEIGHT_TRGM_SIM: float = 0.4
BONUS_CATEGORY_MATCH: float = 0.3
BONUS_DISTRICT_MATCH: float = 0.15

# Truncation length for the description snippet returned to clients.
# Keeps the dialog compact; full description is fetched lazily by the
# future detail endpoint.
SNIPPET_MAX_LEN: int = 200


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateCandidate:
    """A single ranked candidate returned by `find_duplicate_candidates`.

    Fields MUST stay aligned with the `CaseDuplicateCandidateOut`
    Pydantic schema in `app/schemas_case.py`. Adding a field here is a
    CODE REVIEW PIN — confirm it does not violate the anonymity
    invariant (see module docstring).
    """

    case_id: int
    case_number: str
    category_id: int
    title: str
    description_snippet: str
    district_id: str | None
    constituency: str | None
    similarity_score: float
    support_count: int
    submitted_at: datetime


# ---------------------------------------------------------------------------
# Internal row shape — pure-functional scoring helper input.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CandidateRow:
    """Minimal in-memory representation of a Case row for scoring.

    The query returns rows in this shape so the scoring logic doesn't
    depend on ORM loading semantics.
    """

    case_id: int
    case_number: str
    category_id: int
    title: str
    description: str
    district_id: str | None
    constituency: str | None
    submitted_at: datetime
    fts_rank: float
    trgm_sim: float


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------


def _score_candidate(
    row: _CandidateRow,
    *,
    req_category_id: int,
    req_district_id: str | None,
) -> DuplicateCandidate:
    """Compute one candidate's composite score and produce the output row."""
    score = (
        row.fts_rank * WEIGHT_FTS_RANK
        + row.trgm_sim * WEIGHT_TRGM_SIM
        + (BONUS_CATEGORY_MATCH if row.category_id == req_category_id else 0.0)
        + (BONUS_DISTRICT_MATCH if req_district_id and row.district_id == req_district_id else 0.0)
    )
    snippet = (row.description or "")[:SNIPPET_MAX_LEN]
    return DuplicateCandidate(
        case_id=row.case_id,
        case_number=row.case_number,
        category_id=row.category_id,
        title=row.title,
        description_snippet=snippet,
        district_id=row.district_id,
        constituency=row.constituency,
        similarity_score=round(score, 4),
        support_count=0,  # populated by _attach_support_counts
        submitted_at=row.submitted_at,
    )


# ---------------------------------------------------------------------------
# Query + rank
# ---------------------------------------------------------------------------


def _rank_candidates(
    rows: Iterable[_CandidateRow],
    *,
    req_category_id: int,
    req_district_id: str | None,
) -> list[DuplicateCandidate]:
    """Score + sort candidates. Pure function — DB-free for tests."""
    scored: list[DuplicateCandidate] = [
        _score_candidate(r, req_category_id=req_category_id, req_district_id=req_district_id)
        for r in rows
    ]
    # Sort: composite score DESC, then submitted_at DESC, then id DESC
    # (deterministic — same inputs always produce the same order).
    scored.sort(
        key=lambda c: (
            -c.similarity_score,
            -c.submitted_at.timestamp(),
            -c.case_id,
        )
    )
    return scored


async def _fetch_candidate_rows(
    db: AsyncSession,
    *,
    query_text: str,
    req_category_id: int,
    req_district_id: str | None,
    window_days: int,
    limit: int,
) -> list[_CandidateRow]:
    """Run the FTS + trigram query and return raw candidate rows.

    SQL contract:

      - Restricts to non-terminal statuses (NOT IN withdrawn/rejected/closed)
      - Restricts to the last `window_days` (defaults to 30)
      - Includes rows where EITHER:
          * tsvector @@ tsquery  (lexical match)
          * similarity(title || ' ' || description, query_text) > TRGM_THRESHOLD
      - Returns: id, case_number, category_id, title, description,
        district_id, constituency, submitted_at, fts_rank, trgm_sim
      - Order: fts_rank + trgm_sim + bonuses DESC, then submitted_at DESC
      - Limit: `limit` (default 5)

    NOTE on `query_text` parameterisation:
      We pass the user-supplied text as a single `$1` bind parameter
      wrapped in `plainto_tsquery('english', $1)`. `plainto_tsquery`
      treats its argument as plain text (no tsquery operators), so the
      user input cannot inject tsquery operators. This is the same
      pattern used in `app/routers/search.py` for user-search.
    """
    # Build the SQL. The terminal-status list is inlined; safe because
    # it's a module constant, not user input.
    terminal_list = ", ".join(f"'{s}'" for s in sorted(TERMINAL_STATUSES))

    sql = text(f"""
        SELECT
            c.id AS case_id,
            c.case_number AS case_number,
            c.category_id AS category_id,
            c.title AS title,
            c.description AS description,
            c.district_id AS district_id,
            c.constituency AS constituency,
            c.submitted_at AS submitted_at,
            ts_rank(c.search_vector, plainto_tsquery('english', :q)) AS fts_rank,
            similarity(c.title || ' ' || c.description, :q) AS trgm_sim
        FROM cases c
        WHERE c.status NOT IN ({terminal_list})
          AND c.submitted_at >= NOW() - make_interval(days => :window_days)
          AND (
              c.search_vector @@ plainto_tsquery('english', :q)
              OR similarity(c.title || ' ' || c.description, :q) > :trgm_threshold
          )
        ORDER BY
            (ts_rank(c.search_vector, plainto_tsquery('english', :q)) * :w_fts
             + similarity(c.title || ' ' || c.description, :q) * :w_trgm
             + CASE WHEN c.category_id = :req_cat THEN :bonus_cat ELSE 0 END
             + CASE WHEN c.district_id = :req_dist THEN :bonus_dist ELSE 0 END
            ) DESC,
            c.submitted_at DESC,
            c.id DESC
        LIMIT :lim
    """)

    result = await db.execute(
        sql,
        {
            "q": query_text,
            "window_days": int(window_days),
            "trgm_threshold": TRGM_THRESHOLD,
            "w_fts": WEIGHT_FTS_RANK,
            "w_trgm": WEIGHT_TRGM_SIM,
            "req_cat": int(req_category_id),
            "req_dist": req_district_id,
            "bonus_cat": BONUS_CATEGORY_MATCH,
            "bonus_dist": BONUS_DISTRICT_MATCH,
            "lim": int(limit),
        },
    )
    return [
        _CandidateRow(
            case_id=row.case_id,
            case_number=row.case_number,
            category_id=row.category_id,
            title=row.title,
            description=row.description,
            district_id=row.district_id,
            constituency=row.constituency,
            submitted_at=row.submitted_at,
            fts_rank=float(row.fts_rank or 0.0),
            trgm_sim=float(row.trgm_sim or 0.0),
        )
        for row in result.all()
    ]


async def _attach_support_counts(
    db: AsyncSession, candidates: list[DuplicateCandidate]
) -> list[DuplicateCandidate]:
    """Populate `support_count` for each candidate.

    ONE additional query (no N+1): `SELECT original_case_id, COUNT(*)
    FROM case_support WHERE original_case_id IN (...) GROUP BY ...`.

    If the input is empty, we skip the query — saves a round-trip on
    the common "no duplicates found" path.
    """
    if not candidates:
        return candidates

    ids = [c.case_id for c in candidates]
    sql = text(
        "SELECT original_case_id, COUNT(*) AS cnt "
        "FROM case_support "
        "WHERE original_case_id = ANY(:ids) "
        "GROUP BY original_case_id"
    )
    result = await db.execute(sql, {"ids": ids})
    counts = {row.original_case_id: int(row.cnt) for row in result.all()}

    return [
        DuplicateCandidate(
            case_id=c.case_id,
            case_number=c.case_number,
            category_id=c.category_id,
            title=c.title,
            description_snippet=c.description_snippet,
            district_id=c.district_id,
            constituency=c.constituency,
            similarity_score=c.similarity_score,
            support_count=counts.get(c.case_id, 0),
            submitted_at=c.submitted_at,
        )
        for c in candidates
    ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def find_duplicate_candidates(
    db: AsyncSession,
    *,
    title: str,
    description: str,
    category_id: int,
    district_id: str | None,
    language: str = "EN",
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> list[DuplicateCandidate]:
    """Return ranked existing cases similar to a would-be new case.

    The function NEVER writes to the DB. It is read-only and may be
    called from any viewer's request — the candidate list itself
    contains no PII (see module docstring).

    Args:
        db: AsyncSession. Caller owns the session.
        title: would-be new case's title.
        description: would-be new case's description.
        category_id: case_categories.id the user selected.
        district_id: free-form district string, optional.
        language: reserved for future language-specific stemmers;
            ignored for v1 (English-only corpus).
        window_days: how far back to look. Defaults to 30.
        limit: max candidates returned. Defaults to 5.

    Returns:
        Up to `limit` DuplicateCandidate rows, ranked by composite
        score (FTS rank + trigram similarity + category/district
        bonuses). The list may be empty — never raises on empty.
    """
    # Server-side concat so the user cannot inject tsquery operators
    # via whitespace tricks; `plainto_tsquery` already protects against
    # operator injection, but the concat keeps the query text compact
    # (better recall on multi-word titles + descriptions).
    query_text = f"{title} {description}".strip()
    if not query_text:
        return []

    rows = await _fetch_candidate_rows(
        db,
        query_text=query_text,
        req_category_id=category_id,
        req_district_id=district_id,
        window_days=window_days,
        limit=limit,
    )
    ranked = _rank_candidates(
        rows,
        req_category_id=category_id,
        req_district_id=district_id,
    )
    with_counts = await _attach_support_counts(db, ranked)

    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "find_duplicate_candidates category_id=%s district_id=%s -> %d candidates",
            category_id,
            district_id,
            len(with_counts),
        )
    return with_counts


__all__ = [  # noqa: RUF022 — items grouped by feature, not alphabetical
    "find_duplicate_candidates",
    "DuplicateCandidate",
    "NON_TERMINAL_STATUSES",
    "TERMINAL_STATUSES",
    "DEFAULT_WINDOW_DAYS",
    "DEFAULT_LIMIT",
    "TRGM_THRESHOLD",
    # Constants exported for tests:
    "WEIGHT_FTS_RANK",
    "WEIGHT_TRGM_SIM",
    "BONUS_CATEGORY_MATCH",
    "BONUS_DISTRICT_MATCH",
    "SNIPPET_MAX_LEN",
]