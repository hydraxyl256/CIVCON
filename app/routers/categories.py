from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from .. import models, schemas
from ..database import get_db
from .permissions import require_role

router = APIRouter(
    prefix="/categories",
    tags=["Categories"]
)

@router.post("/", response_model=schemas.CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category: schemas.CategoryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: schemas.UserOut = Depends(require_role([schemas.Role.CITIZEN, schemas.Role.JOURNALIST]))
):
    # Check if category name exists
    category_query = select(models.Category).where(models.Category.name == category.name)
    category_result = await db.execute(category_query)
    if category_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Category name already exists"
        )
    
    db_category = models.Category(name=category.name)
    db.add(db_category)
    await db.commit()
    await db.refresh(db_category)
    return db_category

@router.get("/", response_model=List[schemas.CategoryResponse])
async def get_categories(
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
    skip: int = 0
):
    categories_query = select(models.Category).offset(skip).limit(limit)
    categories_result = await db.execute(categories_query)
    return categories_result.scalars().all()
