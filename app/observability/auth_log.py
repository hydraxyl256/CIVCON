"""
Structured auth-event logging.

Every authentication-relevant endpoint emits a single structured log line
when something interesting happens. The line is a JSON object (or human
text in dev) so log aggregators (Datadog, Loki, ELK) can index on
`event`.

Events:

- login.success
- login.failed            — bad credentials, user lookup miss, etc.
- login.suspended         — account is_active=False at login time
- signup.success
- signup.failed
- refresh.success
- refresh.reuse_detected  — token theft heuristic; this is HIGH SEVERITY
- refresh.failed
- logout.success
- logout.no_token
- password_reset.requested
- password_reset.completed
- password_reset.email_failed
- oauth.bootstrap_issued
- oauth.exchange.success
- oauth.exchange.failed
- oauth.exchange.replay   — same bootstrap code presented twice
- role.denied

We never log tokens, passwords, or full request bodies. `user_id`,
`email`, `ip`, `user_agent`, `family_id`, and `event` are the typical
fields.
"""
from __future__ import annotations

import logging

# Dedicated logger; routes pick this up via `logging.getLogger(__name__)`.
# The application's root logging config already attaches a JSON handler
# in production and a coloured text handler in dev.
logger = logging.getLogger("CIVCON.auth")


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    user_id: int | None = None,
    email: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    family_id: str | None = None,
    reason: str | None = None,
    **extra,
) -> None:
    """
    Emit a single structured log line.

    Extra keyword arguments are merged into the log payload. Useful for
    things like `provider="google"` on `oauth.bootstrap_issued`.
    """
    payload = {"event": event}
    if user_id is not None:
        payload["user_id"] = user_id
    if email is not None:
        payload["email"] = email
    if ip is not None:
        payload["ip"] = ip
    if user_agent is not None:
        # Truncate to avoid log explosion from long UA strings
        payload["user_agent"] = (user_agent or "")[:200]
    if family_id is not None:
        payload["family_id"] = family_id
    if reason is not None:
        payload["reason"] = reason
    payload.update(extra)

    logger.log(level, event, extra={"auth_event": payload})


__all__ = ["log_event"]