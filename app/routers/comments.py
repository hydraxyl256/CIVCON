from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Query, Body
from app.routers.oauth2 import get_current_user
from app.schemas import CommentResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from pathlib import Path
import uuid
from typing import List, Optional
from .. import models, schemas
from app.routers import oauth2
from ..database import get_db
import cloudinary
import cloudinary.uploader
import os
from app.config import settings
from app.models import Comment, Post, User
from datetime import datetime


# Configure Cloudinary
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
)


router = APIRouter(
    prefix="/comments",
    tags=["Comments"]
)


#  Create Comment
@router.post("/{post_id}/comments", response_model=CommentResponse)
async def create_comment(
    post_id: int,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a new comment on a specific post.
    Accepts JSON: { "content": "your comment" }
    """
    content = payload.get("content")
    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="Comment content is required.")

    #  Ensure post exists
    stmt = select(Post).where(Post.id == post_id)
    post = (await db.execute(stmt)).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    #  Create comment
    db_comment = Comment(
        content=content.strip(),
        author_id=current_user.id,
        post_id=post_id,
        created_at=datetime.utcnow(),
    )

    db.add(db_comment)
    await db.commit()
    await db.refresh(db_comment)
    return db_comment


# Get Comments for a Post
@router.get("/{post_id}", response_model=List[schemas.CommentResponse])
async def get_comments(post_id: int, db: AsyncSession = Depends(get_db)):
    post = (await db.execute(select(models.Post).where(models.Post.id == post_id))).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    #  Preload author + replies + replies' authors 
    query = (
        select(models.Comment)
        .where(models.Comment.post_id == post_id)
        .options(
            selectinload(models.Comment.author),
            selectinload(models.Comment.replies).selectinload(models.Comment.author)
        )
    )
    comments = (await db.execute(query)).scalars().all()
    return comments



@router.get("/", response_model=schemas.NotificationListResponse)
async def list_comments(
    post_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
):

    # Base query
    stmt = select(models.Comment).options(selectinload(models.Comment.author))
    if post_id:
        stmt = stmt.where(models.Comment.post_id == post_id)

    # Count total comments
    total_result = await db.execute(
        select(models.Comment).where(models.Comment.post_id == post_id) if post_id else select(models.Comment)
    )
    total = len(total_result.scalars().all())
    pages = (total + limit - 1) // limit  # ceiling division

    # Apply pagination
    offset = (page - 1) * limit
    result = await db.execute(stmt.offset(offset).limit(limit))
    comments = result.scalars().all()

    pagination = schemas.Pagination(page=page, size=limit, total=total, pages=pages)
    return schemas.NotificationListResponse(data=comments, pagination=pagination)
