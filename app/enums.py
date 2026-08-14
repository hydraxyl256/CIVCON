"""
Case-domain enums.

Single source of truth for every state and action that the case-management
domain uses. Lives outside `app/models.py` so it can be imported by
Pydantic schemas, services, and tests without pulling in SQLAlchemy.

Convention: every value is lowercase snake_case (matches the wire
representation used by the future `/cases/*` API).
"""
from __future__ import annotations

import enum


class CaseStatus(enum.StrEnum):
    """Workflow states for a Case.

    Spec (`Core Case Domain.txt` STEP 4) lists nine active states. Two
    more — WITHDRAWN, REJECTED — are reserved as terminal states that the
    DB accepts but the v1 service layer does not let users reach.
    Future product work (citizen self-service, admin rejection) will
    expose them through the workflow.
    """

    SUBMITTED             = "submitted"
    RECEIVED              = "received"
    ASSIGNED              = "assigned"
    UNDER_REVIEW          = "under_review"
    INFORMATION_REQUESTED = "information_requested"
    CITIZEN_RESPONDED     = "citizen_responded"
    IN_PROGRESS           = "in_progress"
    RESOLVED              = "resolved"
    CLOSED                = "closed"
    # Reserved / future:
    WITHDRAWN             = "withdrawn"
    REJECTED              = "rejected"


class CasePriority(enum.StrEnum):
    """Priority levels. Spec STEP 5."""

    LOW      = "low"
    NORMAL   = "normal"
    HIGH     = "high"
    CRITICAL = "critical"


class CaseTimelineEventType(enum.StrEnum):
    """Customer-visible timeline event types.

    Mirror of case-lifecycle events. Every status transition auto-writes
    one of these rows (services/cases/workflow.py::apply_transition).
    """

    STATUS_CHANGED   = "status_changed"
    RESPONSE_ADDED   = "response_added"
    ATTACHMENT_ADDED = "attachment_added"
    ASSIGNED         = "assigned"
    REASSIGNED       = "reassigned"
    PRIORITY_CHANGED = "priority_changed"
    CATEGORY_CHANGED = "category_changed"
    CASE_CREATED     = "case_created"   # emitted on POST /cases/
    CASE_RESOLVED    = "case_resolved"
    CASE_CLOSED      = "case_closed"
    CASE_WITHDRAWN   = "case_withdrawn"
    INFORMATION_REQUESTED = "information_requested"


class CaseAuditAction(enum.StrEnum):
    """Security/audit actions written to case_audit_log.

    A separate, larger set than CaseTimelineEventType because audit must
    capture actions that are NOT customer-visible (e.g. CASE_VIEWED,
    REPORTER_DECRYPTED). Timeline = what the citizen sees; audit = what
    the security team sees.
    """

    CASE_CREATED        = "case_created"
    STATUS_CHANGED      = "status_changed"
    RESPONSE_ADDED      = "response_added"
    ATTACHMENT_UPLOADED = "attachment_uploaded"
    CASE_ASSIGNED       = "case_assigned"
    CASE_REASSIGNED     = "case_reassigned"
    PRIORITY_CHANGED    = "priority_changed"
    CASE_WITHDRAWN      = "case_withdrawn"
    CASE_VIEWED         = "case_viewed"
    CASE_RESOLVED       = "case_resolved"
    CASE_CLOSED         = "case_closed"
    CASE_UNASSIGNED     = "case_unassigned"
    INFORMATION_REQUESTED = "information_requested"
    EVIDENCE_EXPORTED   = "evidence_exported"
    REPORTER_DECRYPTED  = "reporter_decrypted"


# ---------------------------------------------------------------------------
# Role strings used in CaseResponse.author_role and timeline/audit
# actor_role columns. Kept here so we have one place to extend them.
# ---------------------------------------------------------------------------
ROLE_CITIZEN = "citizen"
ROLE_MP = "mp"
ROLE_ADMIN = "admin"
ROLE_SYSTEM = "system"


__all__ = [
    "ROLE_ADMIN",
    "ROLE_CITIZEN",
    "ROLE_MP",
    "ROLE_SYSTEM",
    "CaseAuditAction",
    "CasePriority",
    "CaseStatus",
    "CaseTimelineEventType",
]
