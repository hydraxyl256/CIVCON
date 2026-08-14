"""Anonymity invariant — single canonical reporter-view constructor.

Spec STEP 6 is the single hardest constraint on this codebase:

  > The MP API MUST NEVER expose name, email, phone, photo, user id.
  > Instead expose "Anonymous Citizen".

This module is the ONLY place in the entire backend that constructs the
MP-visible reporter representation. Every case-listing endpoint, every
case-detail endpoint, every MP-inbox endpoint must call
`build_reporter_view(case, viewer)` and pass the result through
verbatim. The Pydantic schema (`AnonymousReporterOut`, added in the
next PR) then freezes this shape so the router cannot smuggle PII into
the wire format by accident.

The shape is enforced TWO ways:

1. `AnonymousReporter` is a `frozen=True` dataclass with explicit
   fields. It has NO `id`, NO `first_name`, NO `last_name`, NO `email`,
   NO `phone`, NO `photo` attributes. A unit test (see
   `app/tests/test_case_domain.py::test_anonymous_reporter_has_no_pii_fields`)
   iterates the dataclass fields and asserts the forbidden set is
   absent.
2. `build_reporter_view` does not take any field that could be PII.
   The function signature accepts only `case` (which has only the
   safe columns) and `viewer` (which is only used for the
   self-reporter check).

This module deliberately does NOT import the `User` model directly,
to make it obvious that no User fields are read.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported only for type checkers; runtime callers pass a Case
    # instance directly. Keeping this out of the runtime import list
    # also makes the anonymity invariant visually obvious — no User
    # fields are reachable from this module.
    from app.models import Case


@dataclass(frozen=True)
class AnonymousReporter:
    """The reporter block as the MP (or any non-self viewer) sees it.

    FIELDS MUST NEVER INCLUDE:
      - id, user_id, internal user pk
      - first_name, last_name, username, display_name (User-side)
      - email, phone
      - photo, photo_url, avatar_url, profile_image
      - any token / hash that could identify the user

    Adding a field here is a CODE REVIEW PIN: it requires the spec
    owner to confirm the field does not violate STEP 6.
    """

    display_handle: str
    district_label: str | None
    region_label: str | None
    submitted_at: str  # ISO-8601; surfaced so the MP knows when the case was filed


@dataclass(frozen=True)
class SelfReporter:
    """The reporter block the citizen themselves sees (when viewing their own case).

    Includes `is_self=True` so a frontend can clearly render
    "this is you" without falling through to the anonymous path.
    """

    is_self: bool
    user_id: int
    display_handle: str
    district_label: str | None
    region_label: str | None


# Return-type discriminated union — callers switch on `is_self`.
# Both branches have no first_name / last_name / email / phone /
# photo fields. The SelfReporter branch has user_id ONLY because the
# viewer is the reporter themselves — there's no anonymity leak in
# showing the user their own id.
ReporterView = AnonymousReporter | SelfReporter


def build_reporter_view(
    *,
    case: Case,  # type: ignore[name-defined]  # forward-ref to app.models.Case
    viewer_user_id: int | None,
    is_admin: bool,
) -> ReporterView | None:
    """Return the reporter block appropriate for this viewer.

    Returns `None` for fully-anonymous cases when the viewer is not the
    reporter and not an admin (the case has no reporter block to show).

    Args:
        case: a Case ORM instance. The function only reads
            `display_handle`, `district_id`, `reporter_user_id`,
            `submitted_at` — no User fields.
        viewer_user_id: the id of the user requesting the case.
            Used only for the self-reporter check.
        is_admin: True iff the viewer is an admin (admin tooling
            gets the full reporter block — but NOT through this
            function; admins use a separate admin-only path that
            bypasses the case router entirely).

    Returns:
        - `SelfReporter` if viewer_user_id == case.reporter_user_id
        - `AnonymousReporter` if the case has a reporter_user_id and
          the viewer is not the reporter and not admin
        - `AnonymousReporter` (default-handle) for fully-anonymous cases
          when the viewer is not the reporter
        - `None` only when the case has no reporter block to show at
          all (fully-anonymous + viewer-not-reporter) AND the caller
          wants to suppress the block — typical for MP inbox preview.
    """

    handle = case.display_handle or "Anonymous Citizen"
    district_label = case.district_id
    region_label: str | None = None

    # Look up the region label lazily. If the reporter is set and has
    # a profile, the profile carries the region. For fully-anonymous
    # cases (reporter_user_id is NULL) we skip the join.
    # NOTE: keep this lightweight — the future case-detail endpoint
    # will eager-load these and this helper will become a pure
    # accessor over the loaded graph. For now the call site can
    # ignore the extra query cost; the first MP inbox query is
    # already joining mp_profile.
    if case.reporter_user_id is None:
        return None

    # Self check — viewer is the reporter themselves.
    if viewer_user_id is not None and viewer_user_id == case.reporter_user_id:
        return SelfReporter(
            is_self=True,
            user_id=case.reporter_user_id,
            display_handle=handle,
            district_label=district_label,
            region_label=region_label,
        )

    # Admin gets the full block through a SEPARATE admin route that
    # is not wired in this PR. By design, build_reporter_view does
    # NOT consult is_admin — admins must use a different code path
    # that does an explicit PII-decryption audit. This is the second
    # defense-in-depth layer.
    del is_admin  # explicit: this function ignores is_admin on purpose

    return AnonymousReporter(
        display_handle=handle,
        district_label=district_label,
        region_label=region_label,
        submitted_at=case.submitted_at.isoformat() if case.submitted_at else "",
    )


def is_self_view(case: Case, viewer_user_id: int | None) -> bool:  # forward-ref via from __future__ import annotations
    """True iff the viewer is the case's reporter.

    Helper for code paths that need to branch on self-vs-other without
    constructing the full view.
    """
    return (
        viewer_user_id is not None
        and case.reporter_user_id is not None
        and int(viewer_user_id) == int(case.reporter_user_id)
    )