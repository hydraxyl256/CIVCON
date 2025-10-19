from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Query
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


@router.post("/{post_id}", response_model=schemas.CommentResponse)
async def create_comment(
    post_id: int,
    content: str = Form(...),
    file: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: schemas.UserOut = Depends(oauth2.get_current_user)
):
    # Validate post exists
    result = await db.execute(select(models.Post).where(models.Post.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    # Handle file upload (Cloudinary)
    media_url = None
    if file:
        allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mov'}
        ext = Path(file.filename).suffix.lower()
        if ext not in allowed_extensions:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported file type")

        # Upload directly to Cloudinary
        upload_result = cloudinary.uploader.upload(
            file.file,
            folder="civcon/comments",
            resource_type="auto"  # handles image or video
        )
        media_url = upload_result["secure_url"]

    # Create comment
    db_comment = models.Comment(
        content=content,
        post_id=post_id,
        author_id=current_user.id,
        media_url=media_url
    )
    db.add(db_comment)
    await db.commit()
    await db.refresh(db_comment)

    # Preload relationships before returning
    query = (
        select(models.Comment)
        .where(models.Comment.id == db_comment.id)
        .options(
            selectinload(models.Comment.author),
            selectinload(models.Comment.replies).selectinload(models.Comment.author)
        )
    )
    refreshed_comment = (await db.execute(query)).scalars().first()

    # Optional: notification
    if post.author_id != current_user.id:
        notification = models.Notification(
            user_id=post.author_id,
            message=f"New comment on your post '{post.title}'",
            post_id=post.id
        )
        db.add(notification)
        await db.commit()

    return refreshed_comment


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
