"""Case-status workflow state machine (spec STEP 4).

Nine active states + two reserved terminal states (WITHDRAWN, REJECTED).
Each transition auto-writes a `CaseTimeline` row and (optionally) a
`CaseAuditLog` row in the SAME transaction so the timeline can never
drift from the case's status.

Allowed transitions are encoded as an adjacency list (`ALLOWED_TRANSITIONS`).
Adding a new transition is a one-line change to this module plus a
unit test.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import CaseAuditAction, CaseStatus, CaseTimelineEventType

if TYPE_CHECKING:
    # Imported only for type-checkers; runtime import happens lazily
    # inside ``apply_transition`` to avoid circulars with app.models.
    from app.models import Case


class InvalidTransition(Exception):
    """Raised when a status move is not in ALLOWED_TRANSITIONS."""

    def __init__(self, from_status: CaseStatus, to_status: CaseStatus) -> None:
        super().__init__(
            f"Cannot move case from {from_status.value!r} to {to_status.value!r}"
        )
        self.from_status = from_status
        self.to_status = to_status


# Adjacency list. Each key is the FROM status; each value is the set
# of allowed TO statuses from that state. Terminal states have an
# empty set.
ALLOWED_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.SUBMITTED: frozenset({
        CaseStatus.RECEIVED,
        CaseStatus.WITHDRAWN,
        CaseStatus.REJECTED,
    }),
    CaseStatus.RECEIVED: frozenset({
        CaseStatus.ASSIGNED,
        CaseStatus.WITHDRAWN,
        CaseStatus.REJECTED,
    }),
    CaseStatus.ASSIGNED: frozenset({
        CaseStatus.UNDER_REVIEW,
        CaseStatus.INFORMATION_REQUESTED,
        CaseStatus.WITHDRAWN,
        CaseStatus.REJECTED,
    }),
    CaseStatus.UNDER_REVIEW: frozenset({
        CaseStatus.INFORMATION_REQUESTED,
        CaseStatus.IN_PROGRESS,
        CaseStatus.RESOLVED,
    }),
    CaseStatus.INFORMATION_REQUESTED: frozenset({
        CaseStatus.CITIZEN_RESPONDED,
        CaseStatus.WITHDRAWN,
    }),
    CaseStatus.CITIZEN_RESPONDED: frozenset({
        CaseStatus.UNDER_REVIEW,
        CaseStatus.IN_PROGRESS,
        CaseStatus.WITHDRAWN,
    }),
    CaseStatus.IN_PROGRESS: frozenset({
        CaseStatus.RESOLVED,
        CaseStatus.INFORMATION_REQUESTED,
    }),
    CaseStatus.RESOLVED: frozenset({
        CaseStatus.CLOSED,
        CaseStatus.IN_PROGRESS,
    }),
    # Terminal states — no outgoing transitions.
    CaseStatus.CLOSED: frozenset(),
    CaseStatus.WITHDRAWN: frozenset(),
    CaseStatus.REJECTED: frozenset(),
}


def is_allowed(from_status: CaseStatus, to_status: CaseStatus) -> bool:
    """True iff `from_status -> to_status` is permitted by the workflow."""
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


# ---------------------------------------------------------------------------
# apply_transition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionResult:
    """What `apply_transition` did.

    Returned to the caller so the case router can compose a response
    without re-querying for the freshly-inserted timeline row.
    """

    from_status: CaseStatus
    to_status: CaseStatus
    timeline_event_type: CaseTimelineEventType


async def apply_transition(
    db: AsyncSession,
    *,
    case: Case,  # forward-ref via from __future__ import annotations
    to_status: CaseStatus,
    actor_user_id: int | None,
    actor_role: str,
    description: str | None = None,
    request_id: str | None = None,
) -> TransitionResult:
    """Apply a status transition; auto-write timeline + audit rows.

    The caller is responsible for committing the session. This is
    intentional — the future case router will want to combine the
    transition with other writes (response insertion, assignment
    closure) in one transaction.

    Raises `InvalidTransition` if the move is not in `ALLOWED_TRANSITIONS`.
    The case's `status` column is mutated on the in-memory ORM object
    but NOT flushed — the caller's `db.commit()` is what makes the
    change durable.
    """
    # Imported lazily to avoid a circular import with `app.models`
    # (services <-> models only ever need to touch model classes).
    from app.models import CaseAuditLog, CaseTimeline

    from_status = CaseStatus(case.status) if not isinstance(case.status, CaseStatus) else case.status
    if not is_allowed(from_status, to_status):
        raise InvalidTransition(from_status, to_status)

    # Choose the timeline event type. Status transitions always emit
    # `STATUS_CHANGED`, but the case has a few "named" terminal
    # transitions that surface to the citizen as `CASE_RESOLVED`,
    # `CASE_CLOSED`, etc., for nicer UI.
    timeline_event_type = CaseTimelineEventType.STATUS_CHANGED
    if to_status == CaseStatus.RESOLVED:
        timeline_event_type = CaseTimelineEventType.CASE_RESOLVED
    elif to_status == CaseStatus.CLOSED:
        timeline_event_type = CaseTimelineEventType.CASE_CLOSED
    elif to_status == CaseStatus.WITHDRAWN:
        timeline_event_type = CaseTimelineEventType.CASE_WITHDRAWN

    # Mutate the case. Caller commits.
    case.status = to_status
    if to_status == CaseStatus.RESOLVED:
        case.resolved_at = datetime.now(tz=UTC)

    timeline = CaseTimeline(
        case_id=case.id,
        event_type=timeline_event_type.value,
        from_status=from_status.value,
        to_status=to_status.value,
        actor_role=actor_role,
        actor_user_id=actor_user_id,
        description=description,
    )
    db.add(timeline)

    audit = CaseAuditLog(
        case_id=case.id,
        action=CaseAuditAction.STATUS_CHANGED.value,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=request_id,
        payload={
            "from": from_status.value,
            "to": to_status.value,
            "description": description,
        },
    )
    db.add(audit)

    return TransitionResult(
        from_status=from_status,
        to_status=to_status,
        timeline_event_type=timeline_event_type,
    )
