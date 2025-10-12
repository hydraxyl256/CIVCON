
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime
import africastalking
import logging

from app.database import get_db
from app.models import Message, User, Role
from app.config import settings

router = APIRouter(prefix="/mp", tags=["mp"])

# Africa’s Talking SMS setup
africastalking.initialize(settings.AFRICASTALKING_USERNAME, settings.AFRICASTALKING_API_KEY)
sms = africastalking.SMS
logger = logging.getLogger(__name__)

@router.post("/reply")
async def mp_reply(message_id: int, reply: str, mp: User = Depends(get_db), db: AsyncSession = Depends(get_db)):
    # Fetch message
    result = await db.execute(select(Message).where(Message.id == message_id))
    message = result.scalars().first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    # Optional: Check MP role
    if mp.role != Role.MP:
        raise HTTPException(status_code=403, detail="Only MPs can send replies")

    # Save reply
    message.response = reply
    message.responded_at = datetime.utcnow()
    db.add(message)
    await db.commit()

    # Notify citizen via SMS
    citizen_phone = message.sender.email.split("@")[0]  # assuming phone stored in email format like ussd_25677XXXXXX@civcon.ug
    try:
        sms.send(message=f"MP reply: {reply}", recipients=[citizen_phone])
    except Exception as e:
        logger.error(f"SMS notification error: {e}")

    return {"status": "success", "message": "Reply sent and citizen notified"}
