from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from .. import models
from ..schemas import NotificationType


async def create_and_send_notification(
    db: AsyncSession,
    user_id: int,
    type: NotificationType,
    message: str,
    post_id: int = None,
    group_id: int = None,
):
    from ..main import manager
    """
    Create structured notification in DB and push via WebSocket.
    """
    db_notification = models.Notification(
        user_id=user_id,
        type=type,
        message=message,
        post_id=post_id,
        group_id=group_id,
        created_at=datetime.utcnow(),
        is_read=False,
    )
    db.add(db_notification)
    await db.commit()
    await db.refresh(db_notification)

    # Push via WebSocket
    await manager.send_json(
        user_id,
        {
            "type": db_notification.type,
            "id": db_notification.id,
            "message": db_notification.message,
            "post_id": db_notification.post_id,
            "group_id": db_notification.group_id,
            "created_at": db_notification.created_at.isoformat(),
            "is_read": db_notification.is_read,
        },
    )

    return db_notification
