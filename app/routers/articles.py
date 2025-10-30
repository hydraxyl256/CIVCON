from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from app.database import get_db
from app.models import Article
from app.schemas import ArticleCreate, ArticleOut, ArticleUpdate

router = APIRouter(prefix="/articles", tags=["Articles"])

# GET /articles
@router.get("/", response_model=List[ArticleOut])
async def get_articles(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Article))
    articles = result.scalars().all()
    return articles

# GET /articles/{id}
@router.get("/{id}", response_model=ArticleOut)
async def get_article(id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Article).filter(Article.id == id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article

# POST /articles (admin only)
@router.post("/", response_model=ArticleOut, status_code=status.HTTP_201_CREATED)
async def create_article(article_data: ArticleCreate, db: AsyncSession = Depends(get_db)):
    new_article = Article(**article_data.dict())
    db.add(new_article)
    await db.commit()
    await db.refresh(new_article)
    return new_article

# PUT /articles/{id}
@router.put("/{id}", response_model=ArticleOut)
async def update_article(id: int, article_data: ArticleUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Article).filter(Article.id == id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    for key, value in article_data.dict(exclude_unset=True).items():
        setattr(article, key, value)

    await db.commit()
    await db.refresh(article)
    return article

# DELETE /articles/{id}
@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_article(id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Article).filter(Article.id == id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    await db.delete(article)
    await db.commit()
    return None
