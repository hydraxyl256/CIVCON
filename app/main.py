from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict
import json
import logging
from datetime import datetime
from .database import engine, get_db, Base
from . import models
from .routers import users, posts, auth, vote, search, comments, groups, categories, notifications, messages, admin
from .routers.oauth2 import get_current_user
from .routers.ussd import router as ussd_router
# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# App and CORS
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(users.router)
app.include_router(posts.router)
app.include_router(auth.router)
app.include_router(vote.router)
app.include_router(search.router)
app.include_router(comments.router)
app.include_router(categories.router)
app.include_router(groups.router)
app.include_router(notifications.router)
app.include_router(messages.router)
app.include_router(admin.router)
app.include_router(ussd_router)


# Database initialization
@app.on_event("startup")
async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created")

@app.get("/")
def root():
    return {"message": "Hello, world"}


# WebSocket connection manager
class ConnectionManager:
    """Handles real-time WebSocket connections."""
    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"WebSocket connected for user_id: {user_id}")

    def disconnect(self, user_id: int):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"WebSocket disconnected for user_id: {user_id}")

    async def send_message(self, user_id: int, message: dict):
        if user_id in self.active_connections:
            await self.active_connections[user_id].send_json(message)
            logger.info(f"Sent WebSocket message to user_id={user_id}: {message}")

# Global connection manager
manager = ConnectionManager()


# WebSocket for notifications
@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket, token: str = None, db: AsyncSession = Depends(get_db)):
    """WebSocket endpoint for real-time notifications."""
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return
    try:
        current_user = await get_current_user(token=token, db=db)
        await manager.connect(websocket, current_user.id)
        try:
            while True:
                await websocket.receive_text()  # keep connection alive
        except WebSocketDisconnect:
            manager.disconnect(current_user.id)
            await websocket.close()
    except Exception as e:
        await websocket.close(code=1008, reason=str(e))


# WebSocket for direct messaging
@app.websocket("/ws/messages/{user_id}")
async def websocket_messaging(websocket: WebSocket, user_id: int, token: str = None, db: AsyncSession = Depends(get_db)):
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return
    try:
        current_user = await get_current_user(token=token, db=db)
        if current_user.id != user_id:
            await websocket.close(code=1008, reason="Unauthorized user_id")
            return
        await manager.connect(websocket, user_id)
        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)
                recipient_id = message.get("recipient_id")
                if recipient_id:
                    await manager.send_message(
                        recipient_id,
                        {
                            "type": "message",
                            "from_user_id": user_id,
                            "content": message.get("content"),
                            "created_at": datetime.utcnow().isoformat()
                        }
                    )
        except WebSocketDisconnect:
            manager.disconnect(user_id)
            await websocket.close()
    except Exception as e:
        await websocket.close(code=1008, reason=str(e))
