from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, HTTPException, status
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



#  Auth Helper
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if not email:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise credentials_exception
    return user



# 📩 REST Endpoints
@router.post("/", response_model=MessageResponse)
async def send_message(
    message: MessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Citizen → MP private message send
    """
    if current_user.role != Role.CITIZEN:
        raise HTTPException(status_code=403, detail="Only citizens can send messages")

    result = await db.execute(
        select(User).where(User.id == message.recipient_id, User.role == Role.MP)
    )
    recipient = result.scalar_one_or_none()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient MP not found")

    db_message = Message(
        content=message.content,
        sender_id=current_user.id,
        recipient_id=recipient.id,
        district_id=current_user.district_id,
        created_at=datetime.utcnow(),
    )
    db.add(db_message)
    await db.commit()
    await db.refresh(db_message)

    #  Notification to MP
    await create_and_send_notification(
        db=db,
        user_id=recipient.id,
        message=f"New message from {current_user.first_name} {current_user.last_name}",
    )

    #  If MP is online, send WS push
    if recipient.id in manager.active_connections:
        await manager.send_message(
            recipient.id,
            {
                "type": "dm",
                "from_user_id": current_user.id,
                "sender_name": f"{current_user.first_name} {current_user.last_name}",
                "content": message.content,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

    logger.info(f"DM: {current_user.email} → {recipient.email}")

    return db_message


@router.get("/conversation/{with_user_id}", response_model=List[MessageResponse])
async def get_conversation(
    with_user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get chat history between logged-in user and another user
    """
    result = await db.execute(
        select(Message)
        .where(
            ((Message.sender_id == current_user.id) & (Message.recipient_id == with_user_id))
            | ((Message.sender_id == with_user_id) & (Message.recipient_id == current_user.id))
        )
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()
    return messages


@router.get("/received", response_model=List[MessageResponse])
async def list_received_messages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    MPs can view all received messages
    """
    if current_user.role != Role.MP:
        raise HTTPException(status_code=403, detail="Only MPs can view received messages")

    result = await db.execute(select(Message).where(Message.recipient_id == current_user.id))
    messages = result.scalars().all()
    return messages



#  WebSocket (Real-Time DM)
@router.websocket("/ws/direct/{user_id}")
async def websocket_direct_chat(websocket: WebSocket, user_id: int):
    """
    WebSocket channel for 1-on-1 DMs, typing, and seen receipts.
    """
    await manager.connect(user_id, websocket)
    logger.info(f"🟢 User {user_id} connected to WS")

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            if event_type == "dm":
                recipient_id = data.get("recipient_id")
                content = data.get("content")
                sender_name = data.get("sender_name", f"User {user_id}")

                logger.info(f"💬 DM from {user_id} → {recipient_id}: {content}")

                # Forward message to recipient if online
                if recipient_id in manager.active_connections:
                    await manager.send_message(
                        recipient_id,
                        {
                            "type": "dm",
                            "from_user_id": user_id,
                            "sender_name": sender_name,
                            "content": content,
                            "timestamp": datetime.utcnow().isoformat(),
                        },
                    )

                # Acknowledge delivery to sender
                await manager.send_message(
                    user_id,
                    {
                        "type": "delivered",
                        "to_user_id": recipient_id,
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                )

            elif event_type == "typing":
                to_user_id = data.get("to_user_id")
                sender_name = data.get("sender_name", f"User {user_id}")
                if to_user_id in manager.active_connections:
                    await manager.send_message(
                        to_user_id,
                        {
                            "type": "typing",
                            "from_user_id": user_id,
                            "sender_name": sender_name,
                            "timestamp": datetime.utcnow().isoformat(),
                        },
                    )

            elif event_type == "seen":
                to_user_id = data.get("to_user_id")
                if to_user_id in manager.active_connections:
                    await manager.send_message(
                        to_user_id,
                        {
                            "type": "seen",
                            "from_user_id": user_id,
                            "timestamp": datetime.utcnow().isoformat(),
                        },
                    )

    except WebSocketDisconnect:
        manager.disconnect(user_id)
        logger.info(f" User {user_id} disconnected from WS")
    except Exception as e:
        logger.error(f" WS error for user {user_id}: {e}")
        manager.disconnect(user_id)
