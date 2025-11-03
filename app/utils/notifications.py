"""
Centralized Notification Utility

 Works with either local or Redis-backed WebSocket manager
 Stores notification in DB
 Sends real-time WebSocket updates if user is online
 Safe for distributed (multi-instance) environments
"""

from datetime import datetime
from typing import Optional
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal as async_session
from app.models import Notification
from app.config import settings

# Choose correct WebSocket manager dynamically
try:
    # Use Redis-based distributed manager in production
    from app.core.manager_redis import get_manager
    manager = get_manager(redis_url=settings.redis_url)
except ImportError:
    # Fallback to local-only manager (for dev or testing)
    from app.core.manager import manager

logger = logging.getLogger(__name__)


async def notify_user(
    user_id: int,
    message: str,
    link: Optional[str] = None,
):
    """
    Store a notification in DB and emit via WebSocket if user is connected.
    """
    try:
        #  Persist notification in DB
        async with async_session() as db:  
            notif = Notification(
                user_id=user_id,
                message=message,
                link=link,
                created_at=datetime.utcnow(),
                read=False,
            )
            db.add(notif)
            await db.commit()

        #  Prepare payload
        payload = {
            "type": "notification",
            "message": message,
            "link": link,
            "timestamp": datetime.utcnow().isoformat(),
        }

        #  Attempt WebSocket delivery
        if manager and manager.is_connected(user_id):
            await manager.send_message(user_id, payload)
            logger.info(f" Notification sent live to user {user_id}")
        else:
            logger.info(f" Notification stored for offline user {user_id}")

    except Exception as e:
        logger.exception(f" Failed to send notification to user {user_id}: {e}")
