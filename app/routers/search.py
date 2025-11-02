from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.sql import func
from sqlalchemy.orm import selectinload
from .. import models, schemas
from app.database import get_db

router = APIRouter(
    prefix="/search",
    tags=["Search"]
)

@router.get("/", response_model=List[schemas.SearchItem])
async def global_search(query: str, db: AsyncSession = Depends(get_db)):
    if not query or len(query.strip()) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters long")

    tsquery = func.plainto_tsquery('english', query)
    results = []

    # Users
    user_stmt = select(models.User).where(models.User.search_vector.op('@@')(tsquery))
    user_result = await db.execute(user_stmt)
    for user in user_result.scalars().all():
        results.append(
            schemas.SearchItem(
                id=user.id,
                type="user",
                name=f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username,
                image=user.profile_image,
            )
        )

    # Posts
    post_stmt = select(models.Post).where(models.Post.search_vector.op('@@')(tsquery))
    post_result = await db.execute(post_stmt)
    for post in post_result.scalars().all():
        results.append(
            schemas.SearchItem(
                id=post.id,
                type="post",
                title=post.title,
                snippet=post.content[:100] if post.content else None,
            )
        )

    # Articles
    article_stmt = select(models.Article).where(models.Article.search_vector.op('@@')(tsquery))
    article_result = await db.execute(article_stmt)
    for article in article_result.scalars().all():
        results.append(
            schemas.SearchItem(
                id=article.id,
                type="article",
                title=article.title,
                snippet=article.summary or article.content[:120],
                image=article.image,
                category=article.category,
            )
        )

    # Comments
    comment_stmt = (
        select(models.Comment.id, models.Comment.content, func.literal("comment").label("type"))
        .where(models.Comment.search_vector.op("@@")(tsquery))
        .limit(5)
    )
    comment_result = await db.execute(comment_stmt)
    for r in comment_result.all():
        results.append({
            "id": r.id,
            "title": r.content[:60] + "...",
            "name": None,
            "type": "comment"
        })

    # Topics 
    try:
        topic_stmt = select(models.Topic).where(
            func.lower(models.Topic.title).like(f"%{query.lower()}%")
        )
        topic_result = await db.execute(topic_stmt)
        for topic in topic_result.scalars().all():
            results.append(
                schemas.SearchItem(
                    id=topic.id,
                    type="topic",
                    title=topic.title,
                    snippet=topic.description,
                    category=topic.category,
                )
            )
    except Exception:
        pass  # ignore if topic model not loaded yet

    # Return aggregated results
    return results

