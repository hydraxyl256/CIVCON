from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from datetime import datetime
import logging
from app.database import get_db
from app.models import User, Message, Role
from app.crud import get_user_by_email
from app.schemas import MessageResponse, MessageCreate
from ..services.notifications import create_and_send_notification
from ..config import settings
from ..core.manager import manager

router = APIRouter(prefix="/messages", tags=["Messages"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm


# Current user dependency
async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user





# Send message
@router.post("/", response_model=MessageResponse)
async def send_message(
    message: MessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.CITIZEN:
        raise HTTPException(status_code=403, detail="Only citizens can send messages")

    # Validate recipient
    result = await db.execute(
        select(User).where(User.id == message.recipient_id, User.role == Role.MP)
    )
    recipient = result.scalar_one_or_none()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient MP not found")

    if current_user.district_id and recipient.district_id and current_user.district_id != recipient.district_id:
        raise HTTPException(status_code=403, detail="Can only message MPs in your district")

    db_message = Message(
        content=message.content,
        sender_id=current_user.id,
        recipient_id=message.recipient_id,
        district_id=current_user.district_id
    )
    db.add(db_message)
    await db.commit()
    await db.refresh(db_message)
    logger.info(f"Message sent from user {current_user.email} to MP {recipient.email}")

    # Send DB notification
    await create_and_send_notification(
        db=db,
        user_id=recipient.id,
        message=f"New message from {current_user.first_name} {current_user.last_name}"
    )

    # Send WebSocket notification if MP is connected
    if recipient.id in manager.active_connections:
        await manager.send_message(
            recipient.id,
            {
                "type": "message",
                "from_user_id": current_user.id,
                "content": message.content,
                "created_at": datetime.utcnow().isoformat()
            }
        )

    return db_message



# List received messages
@router.get("/received", response_model=List[MessageResponse])
async def list_received_messages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != Role.MP:
        raise HTTPException(status_code=403, detail="Only MPs can view received messages")

    result = await db.execute(select(Message).where(Message.recipient_id == current_user.id))
    messages = result.scalars().all()
    return messages
