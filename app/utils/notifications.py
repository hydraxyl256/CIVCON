"""
Centralized Notification Utility

 Works with either local or Redis-backed WebSocket manager
 Stores notification in DB
 Sends real-time WebSocket updates if user is online
 Safe for distributed (multi-instance) environments

.. note::
   Legacy compatibility shim. The Notification ORM model does not
   declare ``link`` or ``read`` columns — the original version of this
   helper passed those kwargs to ``Notification(...)`` and silently
   dropped every event RSVP notification via the swallowed ``TypeError``
   in the ``except`` below (confirmed by direct read during the Aug-2026
   production audit). This rewrite uses ``services/notifications.py``
   and folds the optional ``link`` into the persisted message text so
   the existing call site (``routers/events.py``) keeps working without
   a database migration.
"""

import logging

from app.database import AsyncSessionLocal as async_session
from app.schemas import NotificationType
from app.services.notifications import create_and_send_notification

logger = logging.getLogger(__name__)


async def notify_user(
    user_id: int,
    message: str,
    link: str | None = None,
):
    """
    Store a notification in DB and emit via WebSocket if user is connected.

    ``link`` is preserved by appending an ``Open → <link>`` suffix to the
    message body so the original URL is recoverable by the frontend. A
    future migration may add a dedicated ``link`` column; until then the
    legacy helper stays functional rather than silently failing.
    """
    body = message
    if link:
        body = f"{message}\n\nOpen → {link}"

    try:
        async with async_session() as db:
            await create_and_send_notification(
                db=db,
                user_id=user_id,
                type=NotificationType.SYSTEM,  # generic — callers don't disambiguate
                message=body,
            )
        logger.info("Notification sent to user %s", user_id)
    except Exception:
        logger.exception("Failed to send notification to user %s", user_id)

