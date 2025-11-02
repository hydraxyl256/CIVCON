from datetime import datetime
from app.models import Notification
from app.database import async_session
from app.main import active_connections  

async def notify_user(user_id: int, message: str, link: str | None = None):
    """
    Store notification in DB and emit via WebSocket if user is connected.
    """
    async with async_session() as db:
        notif = Notification(
            user_id=user_id,
            message=message,
            link=link,
            created_at=datetime.utcnow(),
            read=False
        )
        db.add(notif)
        await db.commit()

    # Send real-time message via WebSocket if user is online
    connection = active_connections.get(user_id)
    if connection:
        await connection.send_json({
            "type": "notification",
            "message": message,
            "link": link,
            "timestamp": datetime.utcnow().isoformat()
        })
