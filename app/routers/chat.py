# app/routers/chat.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from typing import Dict, List
from app.routers.oauth2 import get_current_user
from app.models import Message
from app.database import AsyncSessionLocal as async_session
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/ws", tags=["chat"])

# Active connections: {group_id: [list of websockets]}
active_chats: Dict[str, List[WebSocket]] = {}

@router.websocket("/chat/{group_id}")
async def websocket_chat(websocket: WebSocket, group_id: str):
    await websocket.accept()
    if group_id not in active_chats:
        active_chats[group_id] = []
    active_chats[group_id].append(websocket)

    try:
        while True:
            data = await websocket.receive_json()
            sender = data.get("sender", "Anonymous")
            content = data.get("content", "")

            #  Broadcast to group
            for conn in active_chats[group_id]:
                await conn.send_json({
                    "sender": sender,
                    "content": content,
                    "timestamp": datetime.utcnow().isoformat()
                })

            #  Persist message to DB (optional)
            async with async_session() as db:
                msg = Message(
                    group_id=group_id,
                    sender_name=sender,
                    content=content,
                    timestamp=datetime.utcnow()
                )
                db.add(msg)
                await db.commit()
    except WebSocketDisconnect:
        active_chats[group_id].remove(websocket)
        if not active_chats[group_id]:
            del active_chats[group_id]
