"""
DEPRECATED — superseded by the case-management domain (`app.routers.cases`).
This module is kept operational for the legacy frontend pages
(`/discussion/:topicId`, `/live-discussion/:id`, the ModernChatBox DM
widget). Removal is deferred until those pages are migrated. See
`C:\\Users\\HP\\Desktop\\Citizen UI.txt` for the migration plan.
"""
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.realtime import get_connection_manager
from app.core.realtime import wrap as ws_wrap
from app.core.ws_auth import (
    WebSocketAuthError,
    authenticate_ws,
    close_ws_with_auth_error,
)
from app.database import get_db

from .. import models

router = APIRouter(prefix="/ws", tags=["WebSockets"])
logger = logging.getLogger("live_ws")


async def save_live_message(db: AsyncSession, feed_id: int, user_id: int | None, message: str):
    """Optional: persist incoming live message to DB."""
    try:
        db_msg = models.LiveFeedMessage(
            feed_id=feed_id,
            user_id=user_id,
            message=message,
        )
        db.add(db_msg)
        await db.commit()
        await db.refresh(db_msg)
        return db_msg
    except Exception:
        logger.exception("Failed to save live message")
        try:
            await db.rollback()
        except Exception:
            pass
        return None


@router.websocket("/live/{feed_id}")
async def websocket_live_feed(
    websocket: WebSocket,
    feed_id: int,
    token: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """WebSocket endpoint for live feed chat.

    Migrated to the unified connection manager:
      - Auth runs through `authenticate_ws` (4401 on failure).
      - The local `LiveConnectionManager` was replaced with a
        per-feed room in the unified manager. The room is
        broadcast cross-worker via Redis pub/sub, so a message
        sent from worker A reaches a viewer connected to
        worker B (this was a real gap — the old
        LiveConnectionManager only broadcast within one
        worker).
      - The on-the-wire payload is unchanged.
    """
    try:
        current_user = await authenticate_ws(
            token, db, cookies=websocket.cookies
        )
    except WebSocketAuthError as err:
        await close_ws_with_auth_error(websocket, err)
        return

    manager = get_connection_manager()
    await manager.connect(current_user.id, websocket, route="/ws/live/{feed_id}")
    await manager.join_room(f"live:{feed_id}", current_user.id)

    # Notify others that user joined (system event).
    join_payload = ws_wrap({
        "type": "system",
        "event": "join",
        "user": {
            "id": current_user.id,
            "first_name": current_user.first_name,
            "last_name": getattr(current_user, "last_name", None),
            "profile_image": getattr(current_user, "profile_image", None),
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }, type="system")
    await manager.send_room_message(f"live:{feed_id}", join_payload, exclude_user_id=current_user.id)

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            manager.mark_pong(current_user.id)
            try:
                data = json.loads(raw)
            except Exception:
                # Treat raw as message text.
                data = {"message": raw}
            if isinstance(data, dict) and data.get("type") == "pong":
                continue

            message_text = data.get("message") or data.get("text") or ""
            if not message_text:
                continue

            msg_payload = ws_wrap({
                "type": "message",
                "user": {
                    "id": current_user.id,
                    "first_name": current_user.first_name,
                    "last_name": getattr(current_user, "last_name", None),
                    "profile_image": getattr(current_user, "profile_image", None),
                    "role": getattr(current_user, "role", None),
                },
                "message": message_text,
                "timestamp": datetime.now(UTC).isoformat(),
            }, type="message")

            try:
                await save_live_message(db, feed_id, current_user.id, message_text)
            except Exception:
                logger.exception("Could not persist live message")

            await manager.send_room_message(f"live:{feed_id}", msg_payload)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.leave_room(f"live:{feed_id}", current_user.id)
        await manager.disconnect(current_user.id, websocket)
        leave_payload = ws_wrap({
            "type": "system",
            "event": "leave",
            "user": {
                "id": current_user.id,
                "first_name": current_user.first_name,
                "last_name": getattr(current_user, "last_name", None),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }, type="system")
        try:
            await manager.send_room_message(f"live:{feed_id}", leave_payload, exclude_user_id=current_user.id)
        except Exception:
            pass