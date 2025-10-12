from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from app.database import get_db
from app.models import Message

router = APIRouter(prefix="/analytics", tags=["analytics"])

@router.get("/summary")
async def analytics_summary(db: AsyncSession = Depends(get_db)):
    # Questions per topic
    result = await db.execute(
        select(Message.topic, func.count(Message.id)).group_by(Message.topic)
    )
    topics = {row[0]: row[1] for row in result.all()}

    # Questions per district
    result = await db.execute(
        select(Message.district_id, func.count(Message.id)).group_by(Message.district_id)
    )
    districts = {row[0]: row[1] for row in result.all()}

    # Questions per language
    result = await db.execute(
        select(Message.language, func.count(Message.id)).group_by(Message.language)
    )
    languages = {row[0]: row[1] for row in result.all()}

    return {
        "topics": topics,
        "districts": districts,
        "languages": languages
    }
