"""
Analytics summary endpoint.

The historical implementation grouped the now-sunset `messages` table
by topic / district / language. With that table gone, all three
aggregates return empty maps until we wire post-based rollups.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
async def analytics_summary(db: AsyncSession = Depends(get_db)):
    """Return empty analytics aggregates.

    The legacy `messages`-based topic/district/language rollups have
    been sunset along with the chat-era messaging system. Returns
    empty maps so the dashboard still renders cleanly.
    """
    return {
        "topics": {},
        "districts": {},
        "languages": {},
    }