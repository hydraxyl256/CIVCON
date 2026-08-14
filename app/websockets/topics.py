import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.realtime import get_connection_manager
from app.core.realtime import wrap as ws_wrap
from app.database import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


async def _maybe_authenticate(
    token: str | None, db: AsyncSession, cookies: dict | None = None
):
    """`/ws/topics` is a public topic feed, but if the client
    sends a token we still try to authenticate it so the user
    can be joined to a per-user room (and so the broadcast loop
    can exclude the originator if desired).
    """
    if not token:
        # Even with no query token we may still have a cookie — try it.
        pass
    from app.core.ws_auth import WebSocketAuthError, authenticate_ws
    try:
        return await authenticate_ws(token, db, cookies=cookies)
    except WebSocketAuthError:
        return None  # public feed — anonymous OK


@router.websocket("/ws/topics")
async def websocket_topics(websocket: WebSocket, token: str | None = None, db: AsyncSession = Depends(get_db)):
    """Real-time topic updates.

    Migrated to the unified connection manager: the bare
    `connected_clients: List[WebSocket]` was replaced with a
    `topics` room so cross-worker broadcast works correctly
    (the old list was in-memory and lost on every deploy).
    """
    current_user = await _maybe_authenticate(token, db, cookies=websocket.cookies)
    if current_user is not None:
        # Per-user connection for granular routing.
        user_id = current_user.id
    else:
        # Anonymous: use a sentinel user_id for the broadcast room.
        # -1 is reserved so we don't collide with real users.
        user_id = -1

    manager = get_connection_manager()
    await manager.connect(user_id, websocket, route="/ws/topics")
    await manager.join_room("topics", user_id)

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            manager.mark_pong(user_id)
            try:
                json.loads(raw)  # validate, discard
            except Exception:
                pass
    finally:
        await manager.leave_room("topics", user_id)
        await manager.disconnect(user_id, websocket)


async def broadcast_new_topic(topic: dict[str, Any]) -> None:
    """Send a new topic to every connected client.

    Replaces the old in-memory list iteration with the
    manager's room broadcast, which is now cross-worker via
    Redis pub/sub. The on-the-wire payload is unchanged.
    """
    message = {"event": "new_topic", "topic": topic}
    manager = get_connection_manager()
    payload = ws_wrap(message, type="topic")
    await manager.send_room_message("topics", payload)