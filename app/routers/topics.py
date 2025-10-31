from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    BackgroundTasks,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, or_, desc, case, cast, Float
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime, timedelta
from .. import models, schemas
from app.database import get_db
from app.websockets.topics import broadcast_new_topic  


router = APIRouter(
    prefix="/topics",
    tags=["Topics"],
)


# 🚀 CREATE TOPIC
@router.post("/", response_model=schemas.TopicOut)
async def create_topic(
    topic_in: schemas.TopicCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Create a new discussion topic."""
    #  Prevent duplicate titles
    existing = await db.execute(
        select(models.Topic).where(func.lower(models.Topic.title) == topic_in.title.lower())
    )
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Topic with this title already exists")

    new_topic = models.Topic(
        title=topic_in.title.strip(),
        description=topic_in.description.strip(),
        category=topic_in.category.strip() if topic_in.category else None,
        posts=0,
        trending=False,
    )

    db.add(new_topic)
    await db.commit()
    await db.refresh(new_topic)

    #  WebSocket Broadcast
    background_tasks.add_task(
        broadcast_new_topic,
        {
            "id": new_topic.id,
            "title": new_topic.title,
            "description": new_topic.description,
            "category": new_topic.category,
            "posts": new_topic.posts,
            "trending": new_topic.trending,
            "created_at": str(new_topic.created_at),
        },
    )

    return new_topic



#  GET TOPICS (With Filters + Search)
@router.get("/", response_model=List[schemas.TopicOut])
async def get_topics(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 9,
    category: Optional[str] = Query(None, description="Filter by category"),
    search: Optional[str] = Query(None, description="Search in title/description"),
    sort: Optional[str] = Query("new", description="Sort by 'new' or 'trending'"),
):
    """Fetch topics with pagination, filters, and optional sorting."""

    query = select(models.Topic)

    if category:
        query = query.where(func.lower(models.Topic.category) == category.lower())

    if search:
        search_expr = f"%{search.lower()}%"
        query = query.where(
            or_(
                func.lower(models.Topic.title).like(search_expr),
                func.lower(models.Topic.description).like(search_expr),
            )
        )

    # Trending sort = posts + recency
    if sort == "trending":
        now = datetime.utcnow()
        hours_ago_expr = cast(
            func.extract("epoch", now - models.Topic.created_at) / 3600, Float
        )

        trending_score = (models.Topic.posts * 5) + case(
            (hours_ago_expr < 12, 30),
            (hours_ago_expr < 24, 15),
            (hours_ago_expr < 48, 10),
            (hours_ago_expr < 96, 5),
            else_=0,
        )

        query = query.order_by(desc(trending_score))
    else:
        query = query.order_by(desc(models.Topic.created_at))

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    topics = result.scalars().all()

    # Update `trending=True` flag for front-end hints
    for topic in topics:
        topic.trending = topic.posts > 5  # heuristic: >5 posts = trending

    return topics



#  GET TRENDING TOPICS (Dedicated)
@router.get("/trending", response_model=List[schemas.TopicOut])
async def get_trending_topics(
    db: AsyncSession = Depends(get_db),
    limit: int = 6,
):
    """Return the top trending topics, calculated by posts + recency."""
    now = datetime.utcnow()
    hours_ago_expr = cast(
        func.extract("epoch", now - models.Topic.created_at) / 3600, Float
    )

    trending_score = (models.Topic.posts * 5) + case(
        (hours_ago_expr < 12, 30),
        (hours_ago_expr < 24, 15),
        (hours_ago_expr < 48, 10),
        (hours_ago_expr < 96, 5),
        else_=0,
    )

    query = (
        select(models.Topic)
        .order_by(desc(trending_score))
        .limit(limit)
    )
    result = await db.execute(query)
    topics = result.scalars().all()

    # Mark them as trending for frontend
    for t in topics:
        t.trending = True
    return topics



#  GET SINGLE TOPIC
@router.get("/{topic_id}", response_model=schemas.TopicOut)
async def get_topic(topic_id: int, db: AsyncSession = Depends(get_db)):
    """Retrieve a single topic by ID."""
    result = await db.execute(
        select(models.Topic)
        .options(selectinload(models.Topic.posts))
        .where(models.Topic.id == topic_id)
    )
    topic = result.scalars().first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    return topic



#  UPDATE TOPIC
@router.put("/{topic_id}", response_model=schemas.TopicOut)
async def update_topic(
    topic_id: int, topic_in: schemas.TopicUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a topic."""
    result = await db.execute(select(models.Topic).where(models.Topic.id == topic_id))
    topic = result.scalars().first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    if topic_in.title:
        topic.title = topic_in.title.strip()
    if topic_in.description:
        topic.description = topic_in.description.strip()
    if topic_in.category:
        topic.category = topic_in.category.strip()

    await db.commit()
    await db.refresh(topic)
    return topic


#  DELETE TOPIC
@router.delete("/{topic_id}")
async def delete_topic(topic_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a topic."""
    result = await db.execute(select(models.Topic).where(models.Topic.id == topic_id))
    topic = result.scalars().first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    await db.delete(topic)
    await db.commit()
    return {"message": "Topic deleted successfully"}
