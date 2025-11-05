from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict
import json
import logging
from datetime import datetime
from starlette.middleware.sessions import SessionMiddleware
import os

# Internal imports
from app.database import engine, get_db, Base
from app import models
from app.config import settings
from app.routers import (
    users, posts, auth, vote, search, comments, groups, categories,
    notifications, messages, mp, live_feeds, live_ws, articles,
    uploads, topics, follow, events, chat, admin_analytics,
    admin_dashboard, admin_subscriptions, moderation,
    admin_communication
)
from app.routers.ussd import router as ussd_router
from app.websockets import topics as topics_ws
from app.core.manager import manager
from app.routers.oauth2 import get_current_user
from app.core.manager_redis import get_manager
from app.spam_detector import download_nltk_resources


#  Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("CIVCON")


#  FastAPI Application

app = FastAPI(
    title="CIVCON API",
    description=(
        "CIVCON enables Ugandan citizens to directly engage with their MPs "
        "on local issues, fostering transparency, accountability, and civic participation."
    ),
    version="1.0.0"
)

#  CORS Settings
origins = [
    "https://civ-con-sh2j.vercel.app",
    "https://civ-con-front.vercel.app",
    "http://localhost:5173",
    "https://civ-con.org",
    "https://app.civ-con.org"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


#  Session Middleware (Fix: Correct secret_key format)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key or "supersecret_session_key"
)


#  Redis Manager
REDIS_URL = settings.redis_url
manager = get_manager(redis_url=REDIS_URL)


#  Include Routers (API Modules)

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
app.include_router(mp.router)
app.include_router(live_feeds.router)
app.include_router(live_ws.router)
app.include_router(articles.router)
app.include_router(uploads.router)
app.include_router(topics.router)
app.include_router(topics_ws.router)
app.include_router(follow.router)
app.include_router(events.router)
app.include_router(chat.router)
app.include_router(admin_analytics.router)
app.include_router(admin_dashboard.router)
app.include_router(admin_subscriptions.router)
app.include_router(moderation.router)
app.include_router(admin_communication.router)
app.include_router(ussd_router)  


#  Application Startup Events
@app.on_event("startup")
async def on_startup():
    """Initialize database, Redis, and NLTK resources."""
    # Create DB tables if not exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ Database tables initialized")

    # Initialize Redis Manager
    global manager
    manager = get_manager(redis_url=settings.redis_url)
    logger.info("✅ Redis manager initialized")

    # Download NLTK resources for spam detection
    try:
        download_nltk_resources()
        logger.info("✅ NLTK resources ready")
    except Exception as e:
        logger.warning(f"⚠️ NLTK resource setup failed: {e}")


#  Shutdown Event
@app.on_event("shutdown")
async def on_shutdown():
    """Clean shutdown for Redis and background workers."""
    try:
        await manager.stop()
        logger.info("🛑 Redis manager stopped cleanly")
    except Exception as e:
        logger.warning(f"⚠️ Redis manager shutdown error: {e}")


#  Root Endpoint
@app.get("/")
def root():
    return {
        "message": "Welcome to CIVCON API 🚀",
        "status": "running",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "development")
    }



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


