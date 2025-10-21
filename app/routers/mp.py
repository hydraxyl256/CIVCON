from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_
from datetime import datetime
import asyncio
import logging
import africastalking

from app.database import get_db
from app.models import Message, User, MP, Role
from app.config import settings
from app.routers.oauth2 import get_current_user
from app.utils.phone_utils import normalize_phone_number

router = APIRouter(prefix="/mp", tags=["MP"])
logger = logging.getLogger(__name__)

# Initialize Africa’s Talking
africastalking.initialize(settings.AFRICASTALKING_USERNAME, settings.AFRICASTALKING_API_KEY)
sms = africastalking.SMS

# Async SMS sender
async def send_sms_async(phone: str, message: str):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sms.send(message=message, recipients=[phone]))
        logger.info(f"SMS sent to {phone}")
    except Exception as e:
        logger.error(f"Failed to send SMS: {e}")

#  MP Inbox 
@router.get("/inbox")
async def get_inbox(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = 1,
    limit: int = 20,
    search: str = None
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. MPs only.")

    # Get MP record
    result_mp = await db.execute(select(MP).where(MP.user_id == current_user.id))
    mp = result_mp.scalars().first()
    if not mp:
        raise HTTPException(status_code=404, detail="MP profile not found.")

    query = select(Message).where(Message.recipient_id == current_user.id)

    if search:
        query = query.join(User, Message.sender_id == User.id).where(
            or_(
                Message.content.ilike(f"%{search}%"),
                User.first_name.ilike(f"%{search}%"),
                User.last_name.ilike(f"%{search}%")
            )
        )

    query = query.order_by(Message.created_at.asc())
    offset = (page - 1) * limit
    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    messages = result.scalars().all()

    conversations = {}
    for msg in messages:
        sender_id = msg.sender_id
        if sender_id not in conversations:
            conversations[sender_id] = []
        conversations[sender_id].append({
            "id": msg.id,
            "from": msg.sender.first_name if msg.sender else "Unknown",
            "content": msg.content,
            "district": msg.district_id,
            "created_at": msg.created_at,
            "response": msg.response,
            "responded_at": msg.responded_at,
        })

    return {
        "mp": f"{current_user.first_name} {current_user.last_name}",
        "district": mp.district_id,
        "page": page,
        "limit": limit,
        "conversations_count": len(conversations),
        "conversations": conversations
    }

# MP Reply 
@router.post("/reply")
async def mp_reply(
    message_id: int,
    reply: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=403, detail="Only MPs can reply.")

    # Confirm MP record exists
    result_mp = await db.execute(select(MP).where(MP.user_id == current_user.id))
    mp = result_mp.scalars().first()
    if not mp:
        raise HTTPException(status_code=404, detail="MP record not found for this user.")

    # Fetch the original citizen message
    result = await db.execute(select(Message).where(Message.id == message_id))
    orig_msg = result.scalars().first()
    if not orig_msg:
        raise HTTPException(status_code=404, detail="Original message not found.")

    citizen = orig_msg.sender
    if not citizen:
        raise HTTPException(status_code=404, detail="Citizen not found.")

    # Save MP reply as a new message
    reply_msg = Message(
        sender_id=current_user.id,
        recipient_id=citizen.id,
        content=reply,
        district_id=mp.district_id,
        created_at=datetime.utcnow(),
        mp_id=mp.id,
    )
    db.add(reply_msg)

    # Mark original message as responded
    orig_msg.response = reply
    orig_msg.responded_at = datetime.utcnow()
    db.add(orig_msg)
    await db.commit()

    # Send SMS to citizen
    citizen_phone = normalize_phone_number(citizen.phone_number)
    if citizen_phone:
        await send_sms_async(
            citizen_phone,
            f"Reply from MP {current_user.first_name} ({mp.district_id}): {reply}"
        )
    else:
        logger.warning(f"Citizen {citizen.id} has no valid phone number.")

    return {"status": "success", "message": "Reply sent successfully and citizen notified."}

# MP Conversation 
@router.get("/conversation/{citizen_id}")
async def view_conversation(
    citizen_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. MPs only.")

    result = await db.execute(
        select(Message)
        .where(
            ((Message.sender_id == current_user.id) & (Message.recipient_id == citizen_id)) |
            ((Message.sender_id == citizen_id) & (Message.recipient_id == current_user.id))
        )
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()

    conversation = [
        {
            "id": msg.id,
            "from": "You" if msg.sender_id == current_user.id else msg.sender.first_name if msg.sender else "Unknown",
            "content": msg.content,
            "created_at": msg.created_at,
            "response": msg.response,
            "responded_at": msg.responded_at
        }
        for msg in messages
    ]

    return {
        "mp": f"{current_user.first_name} {current_user.last_name}",
        "citizen_id": citizen_id,
        "conversation_count": len(conversation),
        "conversation": conversation
    }
