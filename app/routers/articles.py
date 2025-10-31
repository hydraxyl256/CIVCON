from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import or_, func
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
    search: Optional[str] = Query(None, description="Search by title, summary, or content"),
    lang: Optional[str] = "english",  # optional language for ts vector (default english)
):
    """
    Supports:
      - pagination (skip, limit)
      - filters (category, tag)
      - PostgreSQL full-text search (search) with ranking
    """

    base_select = select(Article).options(selectinload(Article.author))

    # If search param provided -> use full-text search with ranking
    if search and search.strip():
        # build a concatenated text vector: title (higher weight), summary, content
        # weight title as 'A', summary 'B', content 'C' for ranking preference
        tsv_title = func.setweight(func.to_tsvector(lang, func.coalesce(Article.title, "")), 'A')
        tsv_summary = func.setweight(func.to_tsvector(lang, func.coalesce(Article.summary, "")), 'B')
        tsv_content = func.setweight(func.to_tsvector(lang, func.coalesce(Article.content, "")), 'C')

        # combined tsvector
        combined_tsv = tsv_title.op('||')(tsv_summary).op('||')(tsv_content)

        # plainto_tsquery for user search string
        query_ts = func.plainto_tsquery(lang, func.coalesce(search, ''))

        # ranking function
        rank = func.ts_rank_cd(combined_tsv, query_ts)

        # base query that returns only matching rows, ordered by rank desc then published_at
        stmt = (
            select(Article, rank.label("search_rank"))
            .where(combined_tsv.op('@@')(query_ts))
            .options(selectinload(Article.author))
            .order_by(func.coalesce(func.nullif(rank, None), 0).desc(), Article.published_at.desc())
            .offset(skip)
            .limit(limit)
        )

        result = await db.execute(stmt)
        rows = result.all()  # list of (Article, rank)
        articles = [row[0] for row in rows]
        return articles

    # else: regular (no-search) query: apply filters and order by published_at desc
    stmt = (
        base_select.order_by(Article.published_at.desc()).offset(skip).limit(limit)
    )

    if category:
        stmt = stmt.where(Article.category.ilike(f"%{category}%"))
    if tag:
        stmt = stmt.where(Article.tags.contains([tag]))

    result = await db.execute(stmt)
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
