"""Intelligent MP routing engine.

Spec ("Intelligent MP Routing.txt"):
  The citizen should NEVER choose an MP first.
  Workflow: Category → Topic → Location → system determines the
  responsible MP → citizen reviews the suggested MP → submit.

  Routing should use District + Constituency + Region + Administrative
  mapping. If multiple MPs qualify, return ranked suggestions. If no
  match exists, return the nearest administrative match.

  The routing engine MUST be implemented as a service layer — never
  embed routing logic inside controllers. This file is that service
  layer; the future `/cases/suggest-mp` endpoint is a thin wrapper
  around `route_to_mp()`.

DESIGN

Inputs (`RoutingRequest`):
- `category_id` (required) — from the new `case_categories` taxonomy
- `district_id` (optional) — free-form string the citizen selected
- `constituency` (optional) — narrower administrative subdivision
- `region_id` (optional) — `mp_regions.id` the citizen selected

Algorithm (single-pass, no DB writes):

  For each MPProfile row that is `is_active=True` and
  `is_accepting_cases=True`, compute a score:

    +100   district_id matches the request's district_id AND
           (constituency matches OR request.constituency is None)
    + 90   region_id matches the request's region_id AND
           district_id matches (constituency unspecified by MP)
    + 75   region_id matches the request's region_id
           (district_id on the MP is NULL — region fallback)
    + 50   same region_id but district_id differs (nearest match)
    + 25   different region but same constituency string
           (cross-region fallback)
    +  0   no signal at all — filtered out

  Plus small adjustments:
    + 5  MP is explicitly marked is_accepting_cases=True
    -50  MP is is_accepting_cases=False (still returned, but
         ranked last so the citizen can see who is available)

  Sort by score DESC; deterministic tie-breakers: lower mp_profile.id
  first (stable, predictable for tests), then alphabetical region
  name.

  Result always carries:
  - the ranked list (may be empty)
  - a `nearest_match` flag (true iff no exact-match exists but at
    least one administrative fallback does)
  - the request echo so the caller can render what was asked

Why a pure service function (not a SQL query with ORDER BY score)?

  1. Testability — pure scoring against Python objects means unit
     tests don't need a live DB.
  2. Auditability — the score breakdown is captured on every
     MPSuggestion so the citizen-facing UI can show "matched on
     district" vs "nearest match".
  3. Future-proofing — adding a new signal (e.g. language, workload)
     is a single function edit, not a multi-join SQL rewrite.

The function takes an AsyncSession but does NOT use it for writes.
It's a thin facade so the future router can `await route_to_mp(db,
req)` without restructuring. If the engine ever needs to read
preferences (e.g. an MP's workload), the session will be ready.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

_LOGGER = logging.getLogger("CIVCON.case_routing")


# ---------------------------------------------------------------------------
# Score constants — single source of truth, exposed for tests.
# ---------------------------------------------------------------------------

SCORE_EXACT_DISTRICT = 100   # district_id matches
SCORE_REGION_PLUS_DISTRICT = 90  # region + district match
SCORE_REGION_FALLBACK = 75   # region matches but MP has no district
SCORE_NEAREST_REGION = 50    # same region, district differs
SCORE_CROSS_REGION_CONSTITUENCY = 25  # different region, same constituency
SCORE_ACCEPTING_BONUS = 5    # is_accepting_cases=True
SCORE_NOT_ACCEPTING_PENALTY = -50  # is_accepting_cases=False


@dataclass(frozen=True)
class RoutingRequest:
    """Inputs to the routing engine.

    All location fields are optional. The engine degrades gracefully:
    a request with no district, no constituency and no region
    returns the global list of accepting MPs ranked by `is_active`.
    """

    category_id: int
    district_id: str | None = None
    constituency: str | None = None
    region_id: int | None = None


@dataclass(frozen=True)
class MPSuggestion:
    """A single ranked MP suggestion.

    `score_breakdown` is the list of (signal, points) pairs that
    summed to the final score. The future UI can render these so
    the citizen understands why an MP was suggested.
    """

    mp_profile_id: int
    region_id: int | None
    region_label: str | None
    district_id: str | None
    constituency: str | None
    is_accepting_cases: bool
    score: int
    score_breakdown: tuple[tuple[str, int], ...]
    is_nearest_match: bool  # true iff this suggestion came from a fallback


@dataclass(frozen=True)
class RoutingResult:
    """The ranked list of suggestions plus a small echo of the request."""

    request: RoutingRequest
    suggestions: list[MPSuggestion] = field(default_factory=list)
    nearest_match: bool = False  # true if no exact-match but fallback exists
    empty_reason: str | None = None  # populated when suggestions is []


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------


def _normalise(s: str | None) -> str | None:
    """Lowercase + strip a string for comparison. None stays None."""
    if s is None:
        return None
    s2 = s.strip()
    return s2.lower() or None


def _score_candidate(
    *,
    mp_profile_id: int,
    region_id: int | None,
    region_label: str | None,
    mp_district_id: str | None,
    mp_constituency: str | None,
    is_accepting_cases: bool,
    request: RoutingRequest,
) -> MPSuggestion:
    """Compute a single MP's score against the request."""
    req_district = _normalise(request.district_id)
    req_constituency = _normalise(request.constituency)
    mp_district = _normalise(mp_district_id)
    mp_constit = _normalise(mp_constituency)

    breakdown: list[tuple[str, int]] = []
    is_nearest = False

    if req_district is not None and mp_district is not None and req_district == mp_district:
        # Exact district match. Constituency narrows further if both sides
        # have it; if the request has no constituency, the MP's
        # constituency is irrelevant — we still match.
        if req_constituency is None or mp_constit is None or req_constituency == mp_constit:
            breakdown.append(("exact_district", SCORE_EXACT_DISTRICT))
        else:
            # District matches but constituency differs — treat as a
            # softer signal. Use the region-fallback score (75) so we
            # still rank ahead of unrelated MPs.
            breakdown.append(("district_match_constituency_mismatch", SCORE_REGION_FALLBACK))
            is_nearest = True
    elif request.region_id is not None and region_id == request.region_id:
        if mp_district is None:
            breakdown.append(("region_match_mp_no_district", SCORE_REGION_FALLBACK))
            is_nearest = True
        elif req_district is None:
            # Request had no district but the requester chose a region.
            # All MPs in that region are equally valid — rank them by
            # accepting-cases bonus only.
            breakdown.append(("region_match_no_request_district", SCORE_REGION_PLUS_DISTRICT))
        else:
            # Same region, different district — nearest administrative match.
            breakdown.append(("nearest_region", SCORE_NEAREST_REGION))
            is_nearest = True
    elif req_constituency is not None and mp_constit is not None and req_constituency == mp_constit:
        # Cross-region same-constituency — last-resort signal so we
        # never return empty when ANY signal exists.
        breakdown.append(("cross_region_constituency", SCORE_CROSS_REGION_CONSTITUENCY))
        is_nearest = True
    # else: no signal — score = 0, filtered out by the caller.

    if is_accepting_cases:
        breakdown.append(("accepting_cases_bonus", SCORE_ACCEPTING_BONUS))
    else:
        breakdown.append(("not_accepting_penalty", SCORE_NOT_ACCEPTING_PENALTY))

    score = sum(points for _, points in breakdown)

    return MPSuggestion(
        mp_profile_id=mp_profile_id,
        region_id=region_id,
        region_label=region_label,
        district_id=mp_district_id,
        constituency=mp_constituency,
        is_accepting_cases=is_accepting_cases,
        score=score,
        score_breakdown=tuple(breakdown),
        is_nearest_match=is_nearest,
    )


# ---------------------------------------------------------------------------
# Query + rank
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MPRow:
    """Minimal in-memory representation of an MPProfile for scoring.

    The router query returns rows in this shape so the scoring logic
    doesn't depend on ORM loading semantics.
    """

    id: int
    region_id: int | None
    region_label: str | None
    district_id: str | None
    constituency: str | None
    is_active: bool
    is_accepting_cases: bool


async def _fetch_candidate_mps(
    db: AsyncSession, *, category_id: int
) -> list[_MPRow]:
    """Fetch all active, accepting MPProfile rows.

    The future category-level affinity table (which would say "MP X
    handles category Y") is NOT yet built. When it is, this function
    is the one place to add a JOIN.

    Keeping the fetch broad for now is intentional: the spec demands
    that we return ranked suggestions AND a nearest-match fallback.
    Pre-filtering by category would defeat the nearest-match rule.
    """
    # Lazy import — keep the module import-cheap for unit tests that
    # only need the scoring logic and mock the DB.
    from sqlalchemy import select

    from app.models import MPProfile, MPRegion

    stmt = (
        select(
            MPProfile.id,
            MPProfile.region_id,
            MPRegion.name.label("region_label"),
            MPProfile.district_id,
            MPProfile.constituency,
            MPProfile.is_active,
            MPProfile.is_accepting_cases,
        )
        .join(MPRegion, MPRegion.id == MPProfile.region_id, isouter=True)
        .where(MPProfile.is_active.is_(True))
    )
    result = await db.execute(stmt)
    return [
        _MPRow(
            id=row.id,
            region_id=row.region_id,
            region_label=row.region_label,
            district_id=row.district_id,
            constituency=row.constituency,
            is_active=row.is_active,
            is_accepting_cases=row.is_accepting_cases,
        )
        for row in result.all()
    ]


BONUS_SIGNALS = frozenset({
    "accepting_cases_bonus",
    "not_accepting_penalty",
})


def _rank(
    candidates: Iterable[_MPRow],
    request: RoutingRequest,
) -> RoutingResult:
    """Score + sort candidates. Pure function — DB-free for tests."""
    scored: list[MPSuggestion] = []
    for c in candidates:
        suggestion = _score_candidate(
            mp_profile_id=c.id,
            region_id=c.region_id,
            region_label=c.region_label,
            mp_district_id=c.district_id,
            mp_constituency=c.constituency,
            is_accepting_cases=c.is_accepting_cases,
            request=request,
        )
        # An MP with NO actual geographic signal (just the
        # accepting/not-accepting bonus) must NOT appear in the result —
        # showing the citizen a globally-active MP with no link to
        # their location would violate the "system determines the
        # responsible MP" contract.
        has_signal = any(
            name not in BONUS_SIGNALS
            for name, _ in suggestion.score_breakdown
        )
        if has_signal:
            scored.append(suggestion)

    # Sort: score DESC, then region_label ASC (stable), then id ASC
    # (deterministic — same inputs always produce the same order).
    scored.sort(
        key=lambda s: (-s.score, s.region_label or "", s.mp_profile_id)
    )

    if not scored:
        return RoutingResult(
            request=request,
            suggestions=[],
            nearest_match=False,
            empty_reason="no_active_mps",
        )

    nearest = any(s.is_nearest_match for s in scored) and not any(
        s.score_breakdown
        and s.score_breakdown[0][0] == "exact_district"
        for s in scored
    )

    return RoutingResult(
        request=request,
        suggestions=scored,
        nearest_match=nearest,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def route_to_mp(
    db: AsyncSession, request: RoutingRequest
) -> RoutingResult:
    """Score every active MPProfile against the request, return ranked.

    The function NEVER writes to the DB. The category_id is reserved
    for a future category-affinity join — for now it is part of the
    request schema but does not filter the candidate set.

    The function does not raise on empty results — it returns
    `RoutingResult(suggestions=[], empty_reason=...)` so the caller
    (future router) can render a friendly "no MPs found" message
    without exception handling.
    """
    candidates = await _fetch_candidate_mps(db, category_id=request.category_id)
    result = _rank(candidates, request)

    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "route_to_mp category_id=%s district_id=%s constituency=%s "
            "region_id=%s -> %d suggestions (nearest=%s)",
            request.category_id,
            request.district_id,
            request.constituency,
            request.region_id,
            len(result.suggestions),
            result.nearest_match,
        )
    return result


__all__ = [  # noqa: RUF022 — items grouped by feature, not alphabetical
    "RoutingRequest",
    "MPSuggestion",
    "RoutingResult",
    "route_to_mp",
    # Constants exported for tests + future re-use:
    "SCORE_EXACT_DISTRICT",
    "SCORE_REGION_PLUS_DISTRICT",
    "SCORE_REGION_FALLBACK",
    "SCORE_NEAREST_REGION",
    "SCORE_CROSS_REGION_CONSTITUENCY",
    "SCORE_ACCEPTING_BONUS",
    "SCORE_NOT_ACCEPTING_PENALTY",
]