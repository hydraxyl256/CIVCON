from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import func, literal, Text
from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/search", tags=["Search"])

@router.get("/", response_model=schemas.SearchResponse)
async def global_search(query: str, db: AsyncSession = Depends(get_db)):
    if not query or len(query) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters long")

    tsquery = func.plainto_tsquery("english", query)

    # 🧍‍♂️ Users
    user_stmt = (
        select(models.User, literal("user", type_=Text).label("type"))
        .where(models.User.search_vector.op("@@")(tsquery))
        .limit(5)
    )
    user_result = await db.execute(user_stmt)
    users = [u for u, _ in user_result.all()]

    # 📰 Articles
    article_stmt = (
        select(models.Article, literal("article", type_=Text).label("type"))
        .options(selectinload(models.Article.author))
        .where(models.Article.search_vector.op("@@")(tsquery))
        .limit(5)
    )
    article_result = await db.execute(article_stmt)
    articles = [a for a, _ in article_result.all()]

    # 🧾 Posts
    post_stmt = (
        select(models.Post, literal("post", type_=Text).label("type"))
        .options(selectinload(models.Post.author))
        .where(models.Post.search_vector.op("@@")(tsquery))
        .limit(5)
    )
    post_result = await db.execute(post_stmt)
    posts = [p for p, _ in post_result.all()]

    # 💬 Comments
    comment_stmt = (
        select(models.Comment, literal("comment", type_=Text).label("type"))
        .options(selectinload(models.Comment.author))
        .where(models.Comment.search_vector.op("@@")(tsquery))
        .limit(5)
    )
    comment_result = await db.execute(comment_stmt)
    comments = [c for c, _ in comment_result.all()]

    return schemas.SearchResponse(
        users=users,
        articles=articles,
        posts=posts,
        comments=comments
    )
