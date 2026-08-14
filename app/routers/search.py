import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/search", tags=["Search"])

@router.get("/", response_model=schemas.SearchResponse)
async def global_search(query: str, db: AsyncSession = Depends(get_db)):
    if not query or len(query) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters long")

    tsquery = func.plainto_tsquery("english", query)

    # The four `tsvector @@ tsquery` queries are independent — they hit
    # four different tables with no shared joins. Run them concurrently
    # via `asyncio.gather` so wall-clock cost is the slowest single
    # query, not the sum. Behaviour is unchanged: same response shape,
    # same status codes, same `limit(5)` cap per bucket.

    async def _users():
        stmt = (
            select(models.User)
            .where(models.User.search_vector.op("@@")(tsquery))
            .limit(5)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def _articles():
        stmt = (
            select(models.Article)
            .options(selectinload(models.Article.author))
            .where(models.Article.search_vector.op("@@")(tsquery))
            .limit(5)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def _posts():
        stmt = (
            select(models.Post)
            .options(selectinload(models.Post.author))
            .where(models.Post.search_vector.op("@@")(tsquery))
            .limit(5)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def _comments():
        stmt = (
            select(models.Comment)
            .options(selectinload(models.Comment.author))
            .where(models.Comment.search_vector.op("@@")(tsquery))
            .limit(5)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    users, articles, posts, comments = await asyncio.gather(
        _users(), _articles(), _posts(), _comments()
    )

    return schemas.SearchResponse(
        users=users,
        articles=articles,
        posts=posts,
        comments=comments
    )
