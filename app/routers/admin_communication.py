import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.realtime import get_connection_manager
from app.core.realtime import wrap as ws_wrap
from app.database import get_db
from app.models import Notification, User
from app.routers.permissions import require_admin
from app.utils.email_utils import send_email_background

router = APIRouter(
    prefix="/admin-communication",
    tags=["Admin Communication"],
    dependencies=[Depends(require_admin)],
)


# ======================================================
# 🔒 Require admin or superadmin
# ======================================================
async def admin_required(current_user: User = Depends(require_admin)):
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
    data: dict[str, Any],
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
    data: dict[str, Any],
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
        ts = datetime.now(UTC)
        note = Notification(
            user_id=u.id,
            message=f"{title}: {message}",
            created_at=ts,
        )
        db.add(note)

    # Perf: real-time WebSocket pushes are independent coroutines —
    # fan them out concurrently. Behaviour unchanged (each push still
    # goes through manager.send_message with the same per-user
    # timestamp as before), just much faster for large recipient
    # lists. The new manager exposes `send_message(user_id, payload)`
    # (and keeps `send_personal_message` as a backwards-compat alias
    # for legacy callers).
    manager = get_connection_manager()
    payload = ws_wrap({
        "type": "notification",
        "title": title,
        "message": message,
        "timestamp": datetime.now(UTC).isoformat(),
    }, type="notification")
    await asyncio.gather(
        *(manager.send_message(u.id, payload) for u in users)
    )

    await db.commit()
    return {"message": f"Notification sent to {len(users)} user(s)."}
