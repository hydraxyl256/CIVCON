"""Assignment helpers for the MP queue.

Pure functions for the router to call. Mirrors the pattern in
`services/cases/timeline.py`: no SQLAlchemy session-management concerns,
just emit the right rows.

Anonymity invariant:

  The MPProfile itself is OK to return — its `user_id` is OK in the
  MP-queue route because the calling user is themselves an MP (the
  `user_id` field on MPProfile is the auth linkage, not citizen PII).
  Citizen PII flows are NEVER touched in this module. The MP queue
  endpoint is responsible for projecting the Case's anonymous reporter
  block; this module just resolves the MP's MPProfile for routing context.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MPProfile, User


async def get_viewer_mp_profile(
    db: AsyncSession, *, user: User,
) -> MPProfile | None:
    """Resolve the MPProfile for the current viewer.

    Returns None if the user has no MPProfile. The router is responsible
    for raising 403 if the calling code requires an MPProfile and got
    None back.

    Future refactor: this will be a typed dependency (the MPProfile is
    the routing context for the entire /mp/queue surface).
    """
    profile = (
        await db.execute(select(MPProfile).where(MPProfile.user_id == user.id))
    ).scalar_one_or_none()
    return profile


__all__ = ["get_viewer_mp_profile"]  # pragma: no cover — re-exports
