from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import or_, func, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List, Optional

from app.database import get_db
from app.models import Article
from app.schemas import ArticleCreate, ArticleOut, ArticleUpdate

router = APIRouter(prefix="/articles", tags=["Articles"])

#  GET /articles (with search, category, tag filters)
@router.get("/", response_model=List[ArticleOut])
async def get_articles(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 9,
    category: Optional[str] = None,
    tag: Optional[str] = None,
    search: Optional[str] = None,
    sort: Optional[str] = "latest",  # latest | oldest | relevance
):
    """
    Get paginated, searchable, sortable articles.
    Supports category, tag, and full-text search with rank ordering.
    """

    # Build base query
    query = select(Article).options(selectinload(Article.author))

    # Filtering
    if category:
        query = query.where(Article.category.ilike(f"%{category}%"))
    if tag:
        query = query.where(Article.tags.contains([tag]))

    # Full-text search vector
    if search:
        # Build weighted tsvector
        tsvector = (
            func.setweight(func.to_tsvector("english", func.coalesce(Article.title, "")), "A")
            + func.setweight(func.to_tsvector("english", func.coalesce(Article.summary, "")), "B")
            + func.setweight(func.to_tsvector("english", func.coalesce(Article.content, "")), "C")
        )

        tsquery = func.plainto_tsquery("english", search)
        rank = func.ts_rank_cd(tsvector, tsquery).label("rank")

        # Filter and rank by match
        query = (
            query.add_columns(rank)
            .where(tsvector.op("@@")(tsquery))
        )

        if sort == "relevance":
            query = query.order_by(desc(rank))
        else:
            query = query.order_by(desc(Article.published_at))
    else:
        # Regular ordering when no search term
        if sort == "oldest":
            query = query.order_by(asc(Article.published_at))
        else:
            query = query.order_by(desc(Article.published_at))

    # Pagination
    query = query.offset(skip).limit(limit)

    # Execute
    result = await db.execute(query)

    # If search used rank, the result includes tuples (Article, rank)
    if search:
        articles = [row[0] for row in result.all()]
    else:
        articles = result.scalars().all()

    return articles

#  GET /articles/{id}
@router.get("/{id}", response_model=ArticleOut)
async def get_article(id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Article)
        .options(selectinload(Article.author))
        .where(Article.id == id)
    )
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


#  POST /articles
@router.post("/", response_model=ArticleOut, status_code=status.HTTP_201_CREATED)
async def create_article(article_data: ArticleCreate, db: AsyncSession = Depends(get_db)):
    new_article = Article(**article_data.dict())
    db.add(new_article)
    await db.commit()
    await db.refresh(new_article)

    #  Re-fetch with author preloaded to avoid MissingGreenlet
    result = await db.execute(
        select(Article)
        .options(selectinload(Article.author))
        .where(Article.id == new_article.id)
    )
    article_with_author = result.scalar_one_or_none()
    return article_with_author


#  PUT /articles/{id}
@router.put("/{id}", response_model=ArticleOut)
async def update_article(
    id: int,
    article_data: ArticleUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Article).where(Article.id == id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    for key, value in article_data.dict(exclude_unset=True).items():
        setattr(article, key, value)

    await db.commit()
    await db.refresh(article)

    #  preload author for response
    result = await db.execute(
        select(Article)
        .options(selectinload(Article.author))
        .where(Article.id == article.id)
    )
    return result.scalar_one_or_none()


#  DELETE /articles/{id}
@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_article(id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Article).where(Article.id == id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    await db.delete(article)
    await db.commit()
    return None
