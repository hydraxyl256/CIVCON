from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime
import asyncio
import re
import logging
import africastalking

from app.database import get_db
from app.models import Message, User, Role
from app.config import settings
from .oauth2 import get_current_user

router = APIRouter(prefix="/mp", tags=["MP"])
logger = logging.getLogger(__name__)

# Africa’s Talking SMS setup
africastalking.initialize(settings.AFRICASTALKING_USERNAME, settings.AFRICASTALKING_API_KEY)
sms = africastalking.SMS


# Async SMS helper
async def send_sms_async(phone: str, message: str):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sms.send(message=message, recipients=[phone]))
        logger.info(f"SMS sent to {phone}")
    except Exception as e:
        logger.error(f"Failed to send SMS: {e}")



# 1. MP Inbox — threaded conversation view
@router.get("/inbox")
async def get_inbox(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. MPs only.")

    # Fetch all messages for this MP
    result = await db.execute(
        select(Message)
        .where(Message.recipient_id == current_user.id)
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()

    # Group messages by conversation (by sender)
    conversations = {}
    for msg in messages:
        sender_id = msg.sender_id
        if sender_id not in conversations:
            conversations[sender_id] = []
        conversations[sender_id].append({
            "id": msg.id,
            "from": "You" if msg.sender_id == current_user.id else msg.sender.first_name if msg.sender else "Unknown",
            "content": msg.content,
            "district": msg.district_id,
            "created_at": msg.created_at,
            "response": msg.response,
            "responded_at": msg.responded_at,
            "mp_id": msg.mp_id
        })

    return {
        "mp": f"{current_user.first_name} {current_user.last_name}",
        "conversations_count": len(conversations),
        "conversations": conversations
    }



# 2. MP Reply — reply in threaded conversation
@router.post("/reply")
async def mp_reply(
    message_id: int,
    reply: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=403, detail="Only MPs can reply.")

    # Fetch original message
    result = await db.execute(select(Message).where(Message.id == message_id))
    orig_msg = result.scalars().first()
    if not orig_msg:
        raise HTTPException(status_code=404, detail="Original message not found.")

    citizen = orig_msg.sender
    if not citizen:
        raise HTTPException(status_code=404, detail="Citizen not found.")

    # Save MP reply as a new message in the thread
    reply_msg = Message(
        sender_id=current_user.id,
        recipient_id=citizen.id,
        content=reply,
        district_id=citizen.district_id,
        created_at=datetime.utcnow(),
        mp_id=current_user.id,
        # link to original message for tracking if needed
    )
    db.add(reply_msg)

    # Mark original message as responded (for tracking)
    orig_msg.response = reply
    orig_msg.responded_at = datetime.utcnow()
    db.add(orig_msg)

    await db.commit()

    # Send SMS to citizen
    citizen_phone = citizen.phone_number
    if citizen_phone:
        citizen_phone = re.sub(r"[^+\d]", "", citizen_phone)  # normalize
        await send_sms_async(citizen_phone, f"Reply from MP {current_user.first_name}: {reply}")
    else:
        logger.warning(f"Citizen {citizen.id} has no phone number on record.")

    return {"status": "success", "message": "Reply sent and citizen notified."}



# 3. View full conversation with a citizen
@router.get("/conversation/{citizen_id}")
async def view_conversation(
    citizen_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. MPs only.")

    # Fetch messages between MP and this citizen
    result = await db.execute(
        select(Message)
        .where(
            ((Message.sender_id == current_user.id) & (Message.recipient_id == citizen_id)) |
            ((Message.sender_id == citizen_id) & (Message.recipient_id == current_user.id))
        )
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()

    if not messages:
        return {"conversation": [], "message": "No messages found with this citizen."}

    # Format messages for display
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
