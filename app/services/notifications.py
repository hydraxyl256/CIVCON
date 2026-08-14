from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..core.realtime import get_connection_manager
from ..core.realtime import wrap as ws_wrap
from ..schemas import NotificationType


async def create_and_send_notification(
    db: AsyncSession,
    user_id: int,
    type: NotificationType,
    message: str,
    post_id: int | None = None,
    group_id: int | None = None,
):
    """
    Create structured notification in DB and push via WebSocket.
    """
    db_notification = models.Notification(
        user_id=user_id,
        type=type,
        message=message,
        post_id=post_id,
        group_id=group_id,
        created_at=datetime.now(UTC),
        is_read=False,
    )
    db.add(db_notification)
    await db.commit()
    await db.refresh(db_notification)

    # Push via WebSocket. Use the unified manager (`send_message`
    # auto-wraps the legacy payload in the new envelope).
    manager = get_connection_manager()
    await manager.send_message(
        user_id,
        ws_wrap({
            "type": db_notification.type,
            "id": db_notification.id,
            "message": db_notification.message,
            "post_id": db_notification.post_id,
            "group_id": db_notification.group_id,
            "created_at": db_notification.created_at.isoformat(),
            "is_read": db_notification.is_read,
        }, type=str(db_notification.type)),
    )

    return db_notification
