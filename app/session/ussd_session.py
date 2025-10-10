from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import UssdSession
from datetime import datetime

async def get_session(db: AsyncSession, session_id: str, phone_number: str):
    """Fetch a session if it exists."""
    result = await db.execute(
        select(UssdSession).where(
            UssdSession.session_id == session_id,
            UssdSession.phone_number == phone_number
        )
    )
    return result.scalars().first()


async def create_or_update_session(db: AsyncSession, session_id: str, phone_number: str,
                                   current_step: str, user_data: dict, language: str = "EN"):
    """Create or update a session record."""
    session = await get_session(db, session_id, phone_number)
    if session:
        session.current_step = current_step
        session.user_data = user_data
        session.language = language
    else:
        session = UssdSession(
            session_id=session_id,
            phone_number=phone_number,
            current_step=current_step,
            user_data=user_data,
            language=language
        )
        db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def delete_session(db: AsyncSession, session_id: str):
    """End session after completion."""
    result = await db.execute(
        select(UssdSession).where(UssdSession.session_id == session_id)
    )
    session = result.scalars().first()
    if session:
        await db.delete(session)
        await db.commit()
