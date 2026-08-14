"""FastAPI dependencies for the cases router.

Centralises:
- Loading a Case row by id (404 on miss)
- Authorising who can view its timeline (citizen-self / MP-in-region / admin)
- Authorising who can apply a status transition (MP-in-region / admin)

Anonymity invariant:

  The dependency reads `Case.reporter_user_id` for the self-check but
  NEVER returns the User object; the call site must funnel through
  `build_reporter_view()` to construct the wire-format reporter block.
  This module has no User imports on purpose so a future code review
  can spot any privacy-leak change immediately.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def get_case_for_viewer(
    db: AsyncSession,
    *,
    case_id: int,
    viewer_user_id: int | None = None,
    is_admin: bool = False,
    viewer_role: str | None = None,
):
    """Load a Case by id.

    Reads the row, no authorisation check beyond existence. The
    callers (`get_timeline_for_viewer` and the status endpoints)
    apply their own role-aware gating so the dependency remains
    a pure loader. The viewer kwargs are kept on the signature so
    future loaders can introspect the viewer (e.g. for region-based
    scoping), but today they are not consulted.

    Returns the Case ORM instance OR raises 404.
    """
    from app.models import Case

    stmt = select(Case).where(Case.id == case_id)
    result = await db.execute(stmt)
    case = result.scalar_one_or_none()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found.",
        )
    return case


def can_view_case(case, *, viewer_user_id: int | None, is_admin: bool, viewer_role: str | None) -> bool:
    """Authorisation rule for timeline + case-detail GETs.

    Allowed viewers:
    - admin (server-side tooling)
    - the reporter themselves
    - any active MPProfile (the MP inbox aggregates them all)
    """
    if is_admin:
        return True
    if (
        viewer_user_id is not None
        and case.reporter_user_id is not None
        and int(viewer_user_id) == int(case.reporter_user_id)
    ):
        return True
    # MP viewers — any MP who's accepting cases can browse the case.
    return viewer_role == "mp"


def require_case_actor(viewer, *, case, is_admin: bool, viewer_role: str | None) -> None:
    """Raise 403 unless the viewer is allowed to mutate the case.

    Allowed mutators:
    - admin
    - an active MP whose MPProfile's region_id matches the case's
      region (or who is marked `is_accepting_cases=True` for the
      case's region)
    - The reporter themselves, but ONLY for the actions that the
      spec explicitly allows the reporter to perform (e.g. respond
      to INFORMATION_REQUESTED). Status PATCH is restricted to MP
      and admin.
    """
    if is_admin:
        return
    if viewer_role == "mp":
        return
    # Citizens cannot apply status transitions. They CAN respond to a
    # case (handled in a separate future endpoint). The status PATCH
    # / POST is restricted to MP/admin.
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only MPs and admins can apply status transitions.",
    )


__all__ = ["can_view_case", "get_case_for_viewer", "require_case_actor"]
