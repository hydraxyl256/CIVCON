from fastapi import APIRouter, Depends, HTTPException
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


@router.get("/", response_model=list[schemas.SearchItem])
async def search(query: str, db: AsyncSession = Depends(get_db)):
    """
    Unified global search for users, posts, comments, and articles.
    Uses PostgreSQL full-text search via GIN indices.
    """
    if not query or len(query) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters long")

    tsquery = func.plainto_tsquery('english', query)
    results = []

    #  Users Search
    user_stmt = (
        select(models.User.id, models.User.username, func.literal("user").label("type"))
        .where(models.User.search_vector.op("@@")(tsquery))
        .limit(5)
    )
    user_result = await db.execute(user_stmt)
    for r in user_result.all():
        results.append({
            "id": r.id,
            "name": r.username,
            "title": None,
            "type": "user"
        })

    #  Posts Search
    post_stmt = (
        select(models.Post.id, models.Post.title, func.literal("post").label("type"))
        .where(models.Post.search_vector.op("@@")(tsquery))
        .limit(5)
    )
    post_result = await db.execute(post_stmt)
    for r in post_result.all():
        results.append({
            "id": r.id,
            "title": r.title,
            "name": None,
            "type": "post"
        })

    #  Comments Search
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

    #  Articles Search
    article_stmt = (
        select(models.Article.id, models.Article.title, func.literal("article").label("type"))
        .where(func.to_tsvector("english", models.Article.tsv_document).op("@@")(tsquery))
        .limit(5)
    )
    article_result = await db.execute(article_stmt)
    for r in article_result.all():
        results.append({
            "id": r.id,
            "title": r.title,
            "name": None,
            "type": "article"
        })

    #  Return merged results (flat list)
    return results
