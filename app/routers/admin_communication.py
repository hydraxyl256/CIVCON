from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    BackgroundTasks,
    Query,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import Any, Dict
from datetime import datetime
from sqlalchemy import func

from app.database import get_db
from app.models import User, Message, Notification
from app.routers.oauth2 import get_current_user
from app.utils.email_utils import send_email_background
from app.core.manager import manager  

router = APIRouter(prefix="", tags=["Admin Communication"])


# ======================================================
# 🔒 Require admin or superadmin
# ======================================================
async def admin_required(current_user: User = Depends(get_current_user)):
    if current_user.role not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Admins only.")
    return current_user


# ======================================================
# 👥 GET /users
# ======================================================
@router.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
):
    result = await db.execute(select(User))
    users = result.scalars().all()
    return [
        {
            "id": u.id,
            "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.username,
            "email": u.email,
            "role": u.role or "Citizen",
            "avatar": getattr(u, "avatar_url", None),
            "last_seen": getattr(u, "last_seen", None),
        }
        for u in users
    ]


# ======================================================
# 📧 POST /emails
# ======================================================
@router.post("/emails")
async def send_bulk_email(
    data: Dict[str, Any],
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
):
    to_type = data.get("toType")
    subject = data.get("subject")
    body = data.get("body")

    if not subject or not body:
        raise HTTPException(400, detail="Subject and body are required.")

    stmt = select(User)
    if to_type == "role":
        role = data.get("role")
        if not role:
            raise HTTPException(400, detail="Role required for role-based emails.")
        stmt = stmt.filter(User.role == role)
    elif to_type == "user":
        user_id = data.get("userId")
        if not user_id:
            raise HTTPException(400, detail="User ID required for individual email.")
        stmt = stmt.filter(User.id == user_id)

    result = await db.execute(stmt)
    recipients = result.scalars().all()
    if not recipients:
        raise HTTPException(404, detail="No recipients found.")

    for user in recipients:
        personalized = body.replace("{name}", user.first_name or user.username)
        background_tasks.add_task(
            send_email_background,
            user.email,
            subject,
            personalized,
        )

    return {"message": f"Email sent to {len(recipients)} user(s)."}


# ======================================================
# 🔔 POST /notifications
# ======================================================
@router.post("/notifications")
async def send_notification(
    data: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
):
    to_type = data.get("toType")
    title = data.get("title")
    message = data.get("message")

    if not title or not message:
        raise HTTPException(400, detail="Title and message required.")

    stmt = select(User)
    if to_type == "role":
        role = data.get("role")
        if not role:
            raise HTTPException(400, detail="Role required for role-based notification.")
        stmt = stmt.filter(User.role == role)
    elif to_type == "user":
        user_id = data.get("userId")
        if not user_id:
            raise HTTPException(400, detail="User ID required for direct notification.")
        stmt = stmt.filter(User.id == user_id)

    result = await db.execute(stmt)
    users = result.scalars().all()
    if not users:
        raise HTTPException(404, detail="No recipients found.")

    for u in users:
        note = Notification(
            user_id=u.id,
            message=f"{title}: {message}",
            created_at=datetime.utcnow(),
        )
        db.add(note)

        # Real-time push via WebSocket
        await manager.send_personal_message(
            {
                "type": "notification",
                "title": title,
                "message": message,
                "timestamp": datetime.utcnow().isoformat(),
            },
            u.id,
        )

    await db.commit()
    return {"message": f"Notification sent to {len(users)} user(s)."}


# ======================================================
# 💬 GET /chats
# ======================================================
@router.get("/chats")
async def get_chat_messages(
    userId: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
):
    stmt = (
        select(Message)
        .where(
            (Message.sender_id == current_user.id) & (Message.recipient_id == userId)
            | (Message.sender_id == userId) & (Message.recipient_id == current_user.id)
        )
        .order_by(Message.created_at)
    )
    result = await db.execute(stmt)
    messages = result.scalars().all()

    return [
        {
            "id": m.id,
            "from": "admin" if m.sender_id == current_user.id else "user",
            "text": m.content,
            "timestamp": m.created_at.isoformat(),
        }
        for m in messages
    ]


# ======================================================
# ✉️ POST /chats
# ======================================================
@router.post("/chats")
async def send_chat_message(
    data: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
):
    to_user_id = data.get("toUserId")
    content = data.get("message")

    if not to_user_id or not content:
        raise HTTPException(400, detail="Recipient ID and message content are required.")

    # Verify recipient
    result = await db.execute(select(User).where(User.id == to_user_id))
    recipient = result.scalar_one_or_none()
    if not recipient:
        raise HTTPException(404, detail="Recipient not found.")

    # Create message record
    msg = Message(
        sender_id=current_user.id,
        recipient_id=recipient.id,
        content=content,
        created_at=datetime.utcnow(),
        mp_id=1,  # or your admin MP reference
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    # Real-time WebSocket push
    await manager.send_personal_message(
        {
            "type": "chat_message",
            "from": "admin",
            "text": msg.content,
            "timestamp": msg.created_at.isoformat(),
        },
        recipient.id,
    )

    # Optional in-app notification
    note = Notification(
        user_id=recipient.id,
        message=f"📩 New message from admin: {content[:80]}",
        created_at=datetime.utcnow(),
    )
    db.add(note)
    await db.commit()

    return {
        "message": "Message sent successfully.",
        "data": {
            "id": msg.id,
            "from": "admin",
            "text": msg.content,
            "timestamp": msg.created_at,
        },
    }
