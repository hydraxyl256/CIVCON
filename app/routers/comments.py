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

# Create Comment for a Post
@router.post("/{post_id}/comments", response_model=CommentResponse)
async def create_comment(
    post_id: int,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a comment or reply under a post.
    Accepts JSON: { "content": "...", "parent_id": optional }
    """
    content = payload.get("content")
    parent_id = payload.get("parent_id")

    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="Comment content is required.")

    # Validate post existence
    post = (await db.execute(select(Post).where(Post.id == post_id))).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Validate parent comment (if reply)
    parent_comment = None
    if parent_id:
        parent_comment = (await db.execute(select(Comment).where(Comment.id == parent_id))).scalar_one_or_none()
        if not parent_comment:
            raise HTTPException(status_code=404, detail="Parent comment not found")

    # Create comment
    db_comment = Comment(
        content=content.strip(),
        author_id=current_user.id,
        post_id=post_id,
        parent_id=parent_id,
        created_at=datetime.utcnow(),
    )

    db.add(db_comment)
    await db.commit()
    await db.refresh(db_comment, attribute_names=["author"])

    return CommentResponse.model_validate(db_comment, from_attributes=True, exclude={"replies"})



# Get Comments for a Post
@router.get("/{post_id}/comments", response_model=List[CommentResponse])
async def get_comments(post_id: int, db: AsyncSession = Depends(get_db)):
    """
    Fetch all comments for a post, with nested replies.
    """
    result = await db.execute(
        select(Comment)
        .where(Comment.post_id == post_id, Comment.parent_id.is_(None))
        .options(
            selectinload(Comment.author),
            selectinload(Comment.replies).selectinload(Comment.author),
        )
        .order_by(Comment.created_at.desc())
    )
    comments = result.scalars().unique().all()
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
