# app/routers/live_ws.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query, status, HTTPException
from typing import Dict, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.routers.oauth2 import get_current_user
from .. import models
from datetime import datetime
import logging
import json
from app.config import settings  

router = APIRouter(prefix="/ws", tags=["WebSockets"])
logger = logging.getLogger("live_ws")


class LiveConnectionManager:
    """
    Manage multiple sockets per live feed (room).
    connections: { feed_id: [WebSocket, ...] }
    """
    def __init__(self):
        self.connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, feed_id: int, websocket: WebSocket):
        await websocket.accept()
        self.connections.setdefault(feed_id, []).append(websocket)
        logger.info(f"WS connect: feed={feed_id}, total={len(self.connections[feed_id])}")

    def disconnect(self, feed_id: int, websocket: WebSocket):
        conns = self.connections.get(feed_id)
        if not conns:
            return
        try:
            conns.remove(websocket)
        except ValueError:
            pass
        if len(conns) == 0:
            self.connections.pop(feed_id, None)
        logger.info(f"WS disconnect: feed={feed_id}, remaining={len(self.connections.get(feed_id, []))}")

    async def broadcast(self, feed_id: int, payload: dict):
        conns = list(self.connections.get(feed_id, []))  # snapshot
        disconnected = []
        for ws in conns:
            try:
                await ws.send_json(payload)
            except Exception as e:
                logger.warning(f"Failed to send WS msg to feed={feed_id}: {e}")
                disconnected.append(ws)
        # cleanup any dead sockets
        for ws in disconnected:
            self.disconnect(feed_id, ws)


manager = LiveConnectionManager()


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
    except Exception as e:
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
    """
    WebSocket endpoint for live feed chat.
    Connect with: wss://your-host/ws/live/<feed_id>?token=<JWT>
    """

    # Require token
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return

    # Authenticate using existing helper (get_current_user expects token & db in your project)
    try:
        current_user = await get_current_user(token=token, db=db)
    except Exception as e:
        logger.warning("WS auth failed: %s", e)
        await websocket.close(code=1008, reason="Invalid token")
        return

    # Accept and register
    await manager.connect(feed_id, websocket)

    # Notify others that user joined (system event)
    join_payload = {
        "type": "system",
        "event": "join",
        "user": {
            "id": current_user.id,
            "first_name": current_user.first_name,
            "last_name": getattr(current_user, "last_name", None),
            "profile_image": getattr(current_user, "profile_image", None)
        },
        "timestamp": datetime.utcnow().isoformat(),
    }
    await manager.broadcast(feed_id, join_payload)

    try:
        while True:
            # Wait for text frames (client should send JSON or plain text)
            raw = await websocket.receive_text()

            # Try parse as JSON. Accept both raw string and JSON with keys (e.g. { message: "..." })
            try:
                data = json.loads(raw)
            except Exception:
                # treat raw as message text
                data = {"message": raw}

            message_text = data.get("message") or data.get("text") or ""
            if not message_text:
                # ignore empty messages
                continue

            # Build broadcast payload
            msg_payload = {
                "type": "message",
                "user": {
                    "id": current_user.id,
                    "first_name": current_user.first_name,
                    "last_name": getattr(current_user, "last_name", None),
                    "profile_image": getattr(current_user, "profile_image", None),
                    "role": getattr(current_user, "role", None),
                },
                "message": message_text,
                "timestamp": datetime.utcnow().isoformat(),
            }

            # Optional: save to DB (non-blocking pattern is recommended in high-volume; here it's synchronous await)
            try:
                await save_live_message(db, feed_id, current_user.id, message_text)
            except Exception:
                logger.exception("Could not persist live message")

            # Broadcast to everyone in feed room
            await manager.broadcast(feed_id, msg_payload)

    except WebSocketDisconnect:
        # cleanup
        manager.disconnect(feed_id, websocket)
        leave_payload = {
            "type": "system",
            "event": "leave",
            "user": {
                "id": current_user.id,
                "first_name": current_user.first_name,
                "last_name": getattr(current_user, "last_name", None),
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        await manager.broadcast(feed_id, leave_payload)
    except Exception as e:
        logger.exception("WS live feed error")
        try:
            manager.disconnect(feed_id, websocket)
        except Exception:
            pass
        await websocket.close(code=1011, reason="Internal server error")
