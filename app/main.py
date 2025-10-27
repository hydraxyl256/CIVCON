from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict
import json
import logging
from datetime import datetime
from .database import engine, get_db, Base
from . import models
from starlette.middleware.sessions import SessionMiddleware
import os
from app.routers import users, posts, auth, vote, search, comments, groups, categories, notifications, messages, admin, mp, live_feeds, live_ws
from .routers.oauth2 import get_current_user
from .routers.ussd import router as ussd_router
from .config import settings

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# App and CORS
app = FastAPI(
    title="CIVCON API",
    description="CIVCON is a community-driven forum platform that enables Ugandan citizens to directly engage with their MPs on local issues, fostering transparency, accountability, and collaborative problem-solving. The platform allows citizens to raise concerns, form communities, and receive responses from representatives, while MPs can view constituency-specific complaints and take action. Journalists can contribute by going live to highlight community events.",
    version="1.0.0"


)

origins = [
    "https://civ-con-sh2j.vercel.app",  #  frontend on vercel
    "http://localhost:5173",             #  for local dev if using Vite/React
]


app.add_middleware(
    SessionMiddleware,
    secret_key=(settings.session_secret_key, "supersecret_session_key"),  
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
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
app.include_router(mp.router)
app.include_router(live_feeds.router)   
app.include_router(live_ws.router)


# Database initialization
@app.on_event("startup")
async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created")

@app.get("/")
def root():
    return {"message": "Hello, Welcome to CIVCON API!"}


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
        logger.info(f"🔗 User {current_user.id} connected to /ws/notifications")

        while True:
            try:
                await websocket.receive_text()  # keep-alive
            except WebSocketDisconnect:
                logger.info(f" User {current_user.id} disconnected")
                manager.disconnect(current_user.id)
                break  # Exit the while loop cleanly

    except WebSocketDisconnect:
        # Already handled disconnection — just ensure cleanup
        manager.disconnect(current_user.id)
        logger.info(f"User {current_user.id} disconnected abruptly (1006)")
    except Exception as e:
        logger.error(f"WebSocket error for user {current_user.id if 'current_user' in locals() else '?'}: {e}")
        # Try to close only if still open
        if not websocket.client_state.name == "CLOSED":
            try:
                await websocket.close(code=1008, reason=str(e))
            except Exception:
                pass



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


