from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.sql import func
from sqlalchemy.orm import selectinload
from .. import models, schemas
from ..database import get_db

router = APIRouter(
    prefix="/search",
    tags=["Search"]
)

@router.get("/", response_model=schemas.SearchResponse)
async def search(query: str, db: AsyncSession = Depends(get_db)):
    if not query or len(query) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters long")

    tsquery = func.plainto_tsquery('english', query)

    # Users
    user_stmt = select(models.User).where(models.User.search_vector.op('@@')(tsquery))
    user_result = await db.execute(user_stmt)
    users = user_result.scalars().all()

    # Posts
    post_stmt = select(models.Post).where(models.Post.search_vector.op('@@')(tsquery)).options(
        selectinload(models.Post.owner)
    )
    post_result = await db.execute(post_stmt)
    posts = post_result.scalars().all()

    # Comments
    comment_stmt = select(models.Comment).where(models.Comment.search_vector.op('@@')(tsquery)).options(
        selectinload(models.Comment.author)
    )
    comment_result = await db.execute(comment_stmt)
    comments = comment_result.scalars().all()

    return schemas.SearchResponse(
        users=users,
        posts=posts,
        comments=comments
    )
