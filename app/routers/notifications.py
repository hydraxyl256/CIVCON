from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.realtime import get_connection_manager
from app.core.realtime import wrap as ws_wrap
from app.database import get_db
from app.models import Notification, Role, User
from app.schemas import NotificationBase, NotificationResponse

from .permissions import require_role

router = APIRouter(prefix="/notifications", tags=["Notifications"])


# Helper to send notifications via WebSocket
async def send_ws_notification(user_id: int, notification: dict):
    manager = get_connection_manager()
    if manager.is_connected(user_id):
        # Preserve the original `type` on the envelope so consumers
        # that read `envelope.type` see the same string.
        t = str(notification.get("type", "envelope"))
        await manager.send_message(user_id, ws_wrap(notification, type=t))


# List notifications
# `response_model_exclude_none=True` strips null fields from each
# Notification in the response list. Pure payload-size optimisation
# (saves ~30% on a typical notification) — the existing consumer
# (Header.tsx, DashboardHeader.tsx) reads only the fields it
# cares about and treats absent and null identically.
@router.get(
    "/",
    response_model=list[NotificationResponse],
    response_model_exclude_none=True,
)
async def list_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role([Role.CITIZEN, Role.MP, Role.JOURNALIST, Role.ADMIN])),
    skip: int = 0,
    limit: int = 20
):
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    notifications = result.scalars().all()
    return notifications


# Create notification (manual/admin)
@router.post("/", response_model=NotificationResponse, status_code=status.HTTP_201_CREATED)
async def create_notification(
    payload: NotificationBase,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(lambda: require_role([Role.ADMIN]))
):
    db_notification = Notification(
        user_id=payload.user_id,
        type=payload.type,
        message=payload.message,
        post_id=payload.post_id,
        group_id=payload.group_id,
        is_read=False,
        created_at=datetime.now(UTC)
    )
    db.add(db_notification)
    await db.commit()
    await db.refresh(db_notification)

    # Send WebSocket notification
    await send_ws_notification(db_notification.user_id, {
        "type": db_notification.type.value,
        "message": db_notification.message,
        "created_at": db_notification.created_at.isoformat(),
        "is_read": db_notification.is_read
    })

    return db_notification


# Mark notification as read
@router.patch("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role([Role.CITIZEN, Role.MP, Role.JOURNALIST, Role.ADMIN]))
):
    result = await db.execute(select(Notification).where(Notification.id == notification_id))
    notification = result.scalar_one_or_none()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to mark this notification as read")

    notification.is_read = True
    await db.commit()
    await db.refresh(notification)
    return notification

