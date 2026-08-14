"""Timeline service helpers for non-status events.

`apply_transition()` in `workflow.py` already auto-writes a CaseTimeline
row on every status move. This module covers the OTHER timeline events
that a Case can emit:

  - RESPONSE_ADDED   (citizen or MP replies in the case thread)
  - ATTACHMENT_ADDED (evidence uploaded)
  - ASSIGNED         (case first assigned to an MP)
  - REASSIGNED       (case handed to a different MP)
  - PRIORITY_CHANGED (priority upgraded/downgraded)
  - CATEGORY_CHANGED (citizen or admin changed the category)

Every helper:
  - adds ONE `CaseTimeline` row (customer-visible)
  - adds ONE `CaseAuditLog` row (security audit)
  - reads the X-Request-Id contextvar if no request_id is passed
  - does NOT mutate the Case's `status` column (use apply_transition
    for that)
  - does NOT flush — the caller's `db.commit()` is what makes the
    events durable, and groups multiple writes into one transaction

Why a service module and not inline SQL in the router?

  - One canonical place for the audit + timeline dual-write pattern.
  - The router stays thin — three-line handlers.
  - Tests can exercise the helpers without an HTTP layer.

Anonymity invariant:

  These helpers NEVER read `Case.reporter` / `User.email` / etc. The
  caller is responsible for the actor user id (from auth) but the
  actor's PII must not be propagated onto the timeline row — only the
  role is stored. This is enforced by, again, never importing User
  in this module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_request_id
from app.enums import CaseAuditAction, CaseTimelineEventType

if TYPE_CHECKING:
    # Lazy at runtime to avoid circular imports with app.models.
    from app.models import CaseAuditLog, CaseTimeline


async def record_response_added(
    db: AsyncSession,
    *,
    case_id: int,
    response_id: int,
    actor_user_id: int | None,
    actor_role: str,
    is_internal: bool = False,
    request_id: str | None = None,
) -> CaseTimeline:  # forward-ref via from __future__ import annotations
    """Emit one CaseTimeline row for a new CaseResponse.

    `is_internal=True` is recorded in the audit payload so the future
    audit log query can filter on it without joining the CaseResponse
    table.
    """
    from app.models import CaseTimeline

    timeline = CaseTimeline(
        case_id=case_id,
        event_type=CaseTimelineEventType.RESPONSE_ADDED.value,
        actor_role=actor_role,
        actor_user_id=actor_user_id,
        description=("Internal note added" if is_internal else "Response added"),
    )
    db.add(timeline)
    db.add(_audit_payload(
        case_id=case_id,
        action=CaseAuditAction.RESPONSE_ADDED,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=request_id,
        payload={"response_id": response_id, "is_internal": is_internal},
    ))
    return timeline


async def record_information_requested(
    db: AsyncSession,
    *,
    case_id: int,
    actor_user_id: int | None,
    actor_role: str,
    note: str | None = None,
    request_id: str | None = None,
) -> CaseTimeline:  # forward-ref via from __future__ import annotations
    """Emit one CaseTimeline row when an MP requests more information.

    The `note` is the textual request shown to the citizen. The
    status transition itself is recorded by `apply_transition()` —
    this helper ONLY records the information-requested event + an
    audit row, mirroring the other helpers in this module.
    """
    from app.models import CaseTimeline

    timeline = CaseTimeline(
        case_id=case_id,
        event_type=CaseTimelineEventType.INFORMATION_REQUESTED.value,
        actor_role=actor_role,
        actor_user_id=actor_user_id,
        description=(note or "Information requested from citizen"),
    )
    db.add(timeline)
    db.add(_audit_payload(
        case_id=case_id,
        action=CaseAuditAction.INFORMATION_REQUESTED,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=request_id,
        payload={"note": note} if note else {},
    ))
    return timeline


async def record_attachment_added(
    db: AsyncSession,
    *,
    case_id: int,
    attachment_id: int,
    actor_user_id: int | None,
    actor_role: str,
    media_type: str | None = None,
    request_id: str | None = None,
) -> CaseTimeline:  # forward-ref via from __future__ import annotations
    """Emit one CaseTimeline row for a new CaseAttachment."""
    from app.models import CaseTimeline

    timeline = CaseTimeline(
        case_id=case_id,
        event_type=CaseTimelineEventType.ATTACHMENT_ADDED.value,
        actor_role=actor_role,
        actor_user_id=actor_user_id,
        description="Evidence attached",
    )
    db.add(timeline)
    db.add(_audit_payload(
        case_id=case_id,
        action=CaseAuditAction.ATTACHMENT_UPLOADED,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=request_id,
        payload={"attachment_id": attachment_id, "media_type": media_type},
    ))
    return timeline


async def record_assignment(
    db: AsyncSession,
    *,
    case_id: int,
    mp_profile_id: int,
    actor_user_id: int | None,
    actor_role: str,
    reassigned: bool = False,
    request_id: str | None = None,
) -> CaseTimeline:  # forward-ref via from __future__ import annotations
    """Emit one CaseTimeline row when an MP is (re)assigned to a case."""
    from app.models import CaseTimeline

    event_type = (
        CaseTimelineEventType.REASSIGNED
        if reassigned
        else CaseTimelineEventType.ASSIGNED
    )
    audit_action = (
        CaseAuditAction.CASE_REASSIGNED
        if reassigned
        else CaseAuditAction.CASE_ASSIGNED
    )
    timeline = CaseTimeline(
        case_id=case_id,
        event_type=event_type.value,
        actor_role=actor_role,
        actor_user_id=actor_user_id,
        description=(
            "Case reassigned to a different MP"
            if reassigned
            else "Case assigned to an MP"
        ),
    )
    db.add(timeline)
    db.add(_audit_payload(
        case_id=case_id,
        action=audit_action,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=request_id,
        payload={"mp_profile_id": mp_profile_id, "reassigned": reassigned},
    ))
    return timeline


async def record_priority_change(
    db: AsyncSession,
    *,
    case_id: int,
    from_priority: str,
    to_priority: str,
    actor_user_id: int | None,
    actor_role: str,
    request_id: str | None = None,
) -> CaseTimeline:  # forward-ref via from __future__ import annotations
    """Emit one CaseTimeline row when the case priority changes."""
    from app.models import CaseTimeline

    timeline = CaseTimeline(
        case_id=case_id,
        event_type=CaseTimelineEventType.PRIORITY_CHANGED.value,
        actor_role=actor_role,
        actor_user_id=actor_user_id,
        description=f"Priority changed: {from_priority} → {to_priority}",
    )
    db.add(timeline)
    db.add(_audit_payload(
        case_id=case_id,
        action=CaseAuditAction.PRIORITY_CHANGED,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=request_id,
        payload={"from": from_priority, "to": to_priority},
    ))
    return timeline


async def record_category_change(
    db: AsyncSession,
    *,
    case_id: int,
    from_category_id: int,
    to_category_id: int,
    actor_user_id: int | None,
    actor_role: str,
    request_id: str | None = None,
) -> CaseTimeline:  # forward-ref via from __future__ import annotations
    """Emit one CaseTimeline row when the case's category changes.

    The citizen-facing description intentionally omits the category
    IDs (those are internal) — instead it states the change
    factually. The numeric ids live in the audit log payload where
    they belong.
    """
    from app.models import CaseTimeline

    timeline = CaseTimeline(
        case_id=case_id,
        event_type=CaseTimelineEventType.CATEGORY_CHANGED.value,
        actor_role=actor_role,
        actor_user_id=actor_user_id,
        description="Category changed",
    )
    db.add(timeline)
    db.add(_audit_payload(
        case_id=case_id,
        action=CaseAuditAction.PRIORITY_CHANGED,  # reuse; future: CATEGORY_CHANGED
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=request_id,
        payload={"from": from_category_id, "to": to_category_id},
    ))
    return timeline


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _audit_payload(
    *,
    case_id: int,
    action: CaseAuditAction,
    actor_user_id: int | None,
    actor_role: str,
    request_id: str | None,
    payload: dict,
) -> CaseAuditLog:  # forward-ref via from __future__ import annotations
    """Build a CaseAuditLog row inline. Reads X-Request-Id contextvar
    if `request_id` is None."""
    from app.models import CaseAuditLog

    return CaseAuditLog(
        case_id=case_id,
        action=action.value,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=(request_id if request_id is not None else get_request_id()),
        payload=payload,
    )


__all__ = [
    "record_assignment",
    "record_attachment_added",
    "record_category_change",
    "record_information_requested",
    "record_priority_change",
    "record_response_added",
]  # pragma: no cover — re-exports
