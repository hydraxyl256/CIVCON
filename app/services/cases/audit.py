"""Append-only audit logging for the case domain.

Single entry point: `log_audit_event()`. Every case action that the
future router performs should funnel through here so the X-Request-Id,
actor, and payload are always set the same way.

The audit table (`case_audit_log`) is also protected by a Postgres
trigger (see migration c3c4d5e6f7g8) that blocks UPDATE and DELETE,
so even a stray SQL command cannot tamper with the trail.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_request_id
from app.enums import CaseAuditAction

if TYPE_CHECKING:
    # Imported only for type-checkers; the runtime import is lazy inside
    # ``log_audit_event`` to avoid a circular import with app.models.
    from app.models import CaseAuditLog

# A dedicated logger so log shippers can route audit lines to a
# dedicated retention bucket without parsing the JSON stream.
_AUDIT_LOGGER = logging.getLogger("CIVCON.case_audit")


async def log_audit_event(
    db: AsyncSession,
    *,
    case_id: int,
    action: CaseAuditAction,
    actor_user_id: int | None,
    actor_role: str,
    payload: dict | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> CaseAuditLog:  # forward-ref via from __future__ import annotations
    """Write a row to `case_audit_log` and return the ORM instance.

    The caller is responsible for committing the session. If the
    surrounding transaction rolls back, the audit row goes with it —
    callers that need a guaranteed durable record should commit the
    audit row alone before the business transaction.

    The `request_id` argument is OPTIONAL — when omitted, this
    function reads the `X-Request-Id` contextvar set by
    `RequestIdMiddleware`. Background tasks (cron jobs, the future
    14-day auto-closer) pass `request_id=None` explicitly.
    """
    from app.models import CaseAuditLog  # lazy import — avoid circulars

    rid = request_id if request_id is not None else get_request_id()

    row = CaseAuditLog(
        case_id=case_id,
        action=action.value,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        request_id=rid,
        ip_address=ip_address,
        payload=payload,
    )
    db.add(row)

    # Emit a structured log line in addition to the DB row. The log
    # is the searchable copy (the DB row is the authoritative one).
    # Use a `case_id` structured field so the JSON formatter surfaces
    # it at the top level.
    _AUDIT_LOGGER.info(
        "case_audit case_id=%s action=%s actor_role=%s",
        case_id,
        action.value,
        actor_role,
        extra={
            "case_id": case_id,
            "action": action.value,
            "actor_user_id": actor_user_id,
            "actor_role": actor_role,
            "request_id": rid,
            "ip_address": ip_address,
            "payload": payload or {},
        },
    )

    return row