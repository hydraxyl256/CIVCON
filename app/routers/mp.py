from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime
import africastalking
import logging

from app.database import get_db
from app.models import Message, User, Role
from app.config import settings
from .oauth2 import get_current_user  

router = APIRouter(prefix="/mp", tags=["mp"])

# Africa’s Talking SMS setup
africastalking.initialize(settings.AFRICASTALKING_USERNAME, settings.AFRICASTALKING_API_KEY)
sms = africastalking.SMS
logger = logging.getLogger(__name__)

#  1. MP Inbox — view all messages sent to this MP
@router.get("/inbox")
async def get_inbox(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. MPs only.")

    result = await db.execute(
        select(Message)
        .where(Message.recipient_id == current_user.id)
        .order_by(Message.created_at.desc())
    )
    messages = result.scalars().all()

    if not messages:
        return {"inbox": [], "message": "No messages found."}

    return {
        "mp": f"{current_user.first_name} {current_user.last_name}",
        "inbox_count": len(messages),
        "messages": [
            {
                "id": msg.id,
                "from": msg.sender_id,
                "content": msg.content,
                "district": msg.district_id,
                "created_at": msg.created_at,
                "response": msg.response,
                "responded_at": msg.responded_at
            }
            for msg in messages
        ]
    }


#  2. MP Reply — send reply to citizen
@router.post("/reply")
async def mp_reply(
    message_id: int,
    reply: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=403, detail="Only MPs can reply.")

    # Fetch message
    result = await db.execute(select(Message).where(Message.id == message_id))
    message = result.scalars().first()

    if not message:
        raise HTTPException(status_code=404, detail="Message not found.")

    # Save reply
    message.response = reply
    message.responded_at = datetime.utcnow()
    db.add(message)
    await db.commit()

    # Try to notify citizen
    citizen_email = message.sender.email if message.sender else None
    citizen_phone = None
    if citizen_email and citizen_email.startswith("ussd_") and "@civcon.ug" in citizen_email:
        citizen_phone = citizen_email.split("@")[0].replace("ussd_", "")
    if citizen_phone:
        try:
            sms.send(message=f"MP {current_user.first_name}: {reply}", recipients=[citizen_phone])
        except Exception as e:
            logger.error(f"SMS notification error: {e}")

    return {"status": "success", "message": "Reply sent and citizen notified."}
