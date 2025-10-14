from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List, Optional
from .. import models, schemas
from ..schemas import Role  
from ..database import get_db
from .permissions import require_role

router = APIRouter(
    prefix="/live-feeds",
    tags=["Live Feeds"]
)

@router.get("/", response_model=List[schemas.LiveFeedResponse])
async def get_live_feeds(
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
    skip: int = 0,
    active_only: bool = True  # Optional filter for ongoing streams
):
    query = select(models.LiveFeed).offset(skip).limit(limit)
    if active_only:
        query = query.where(models.LiveFeed.is_active == True)  
    result = await db.execute(query)
    live_feeds = result.scalars().all()
    return live_feeds

@router.get("/{feed_id}", response_model=schemas.LiveFeedResponse)
async def get_live_feed(
    feed_id: int,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(models.LiveFeed).where(models.LiveFeed.id == feed_id)
    )
    live_feed = result.scalar_one_or_none()
    if not live_feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live feed not found")
    return live_feed

@router.post("/", response_model=schemas.LiveFeedResponse, status_code=status.HTTP_201_CREATED)
async def create_live_feed(
    live_feed: schemas.LiveFeedCreate,
    db: AsyncSession = Depends(get_db),
    current_user: schemas.UserOut = Depends(lambda: require_role([Role.JOURNALIST]))  # FIXED: Lambda wrapper for async dep with args
):
    # Optional: Check if journalist is verified or active
    if not current_user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not active")

    # Create live feed
    db_live_feed = models.LiveFeed(
        title=live_feed.title,
        stream_url=live_feed.stream_url,
        description=live_feed.description,
        journalist_id=current_user.id  # Associate with current journalist
    )
    db.add(db_live_feed)
    await db.commit()
    await db.refresh(db_live_feed)
    return db_live_feed

@router.put("/{feed_id}", response_model=schemas.LiveFeedResponse)
async def update_live_feed(
    feed_id: int,
    live_feed_update: schemas.LiveFeedCreate,  # Reuse create for update (or make Update schema)
    db: AsyncSession = Depends(get_db),
    current_user: schemas.UserOut = Depends(lambda: require_role([Role.JOURNALIST]))  # Lambda for update
):
    result = await db.execute(
        select(models.LiveFeed).where(models.LiveFeed.id == feed_id)
    )
    db_live_feed = result.scalar_one_or_none()
    if not db_live_feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live feed not found")
    if db_live_feed.journalist_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to update this feed")
    
    update_data = live_feed_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_live_feed, field, value)
    
    await db.commit()
    await db.refresh(db_live_feed)
    return db_live_feed

@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_live_feed(
    feed_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: schemas.UserOut = Depends(lambda: require_role([Role.JOURNALIST]))  # Lambda for delete
):
    result = await db.execute(
        select(models.LiveFeed).where(models.LiveFeed.id == feed_id)
    )
    live_feed = result.scalar_one_or_none()
    if not live_feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live feed not found")
    if live_feed.journalist_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this feed")
    
    await db.delete(live_feed)
    await db.commit()
    return None