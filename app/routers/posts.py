from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from app.database import get_db
from app.models import User, Post, Comment, Vote, LiveFeed, PostMedia
from app.schemas import PostResponse, PostCreate, CommentResponse, CommentCreate, LiveFeedResponse, LiveFeedCreate, PostMediaOut
import logging
import os
from .oauth2 import get_current_user
from datetime import datetime
import cloudinary
import cloudinary.uploader
from app.config import settings


router = APIRouter(prefix="/posts", tags=["Posts"])
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# Configure Cloudinary
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
)



@router.post("/", response_model=PostResponse)
async def create_post(
    title: str = Form(...),
    content: str = Form(...),
    author_id: int = Form(...),
    district_id: Optional[str] = Form(None),
    media_files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db)
):
    post = Post(title=title, content=content, author_id=author_id, district_id=district_id)
    db.add(post)
    await db.commit()
    await db.refresh(post)

    media_list = []
    if media_files:
        for file in media_files:
            # Upload directly to Cloudinary
            upload_result = cloudinary.uploader.upload(
                file.file,
                folder="civcon/posts",
                resource_type="auto"  # supports image/video
            )
            
            media = PostMedia(
                post_id=post.id,
                media_url=upload_result["secure_url"],  # Cloudinary hosted URL
                media_type=file.content_type
            )
            db.add(media)
            media_list.append(media)
        await db.commit()

    await db.refresh(post)
    return PostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        media=[PostMediaOut.from_orm(m) for m in media_list],
        author_id=post.author_id,
        district_id=post.district_id,
        created_at=post.created_at,
        updated_at=post.updated_at,
        like_count=0
    )



# List posts with like count
@router.get("/", response_model=List[PostResponse])
async def list_posts(skip: int = 0, limit: int = 10, district_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    stmt = select(Post).options(selectinload(Post.votes), selectinload(Post.media))
    if district_id:
        stmt = stmt.filter(Post.district_id == district_id)
    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    posts = result.scalars().unique().all()

    return [
        PostResponse(
            id=p.id,
            title=p.title,
            content=p.content,
            author_id=p.author_id,
            district_id=p.district_id,
            media=[PostMediaOut.from_orm(m) for m in p.media],
            created_at=p.created_at,
            updated_at=p.updated_at,
            like_count=len(p.votes)
        )
        for p in posts
    ]


# Get a single post
@router.get("/{post_id}", response_model=PostResponse)
async def get_post(post_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Post).where(Post.id == post_id).options(selectinload(Post.votes), selectinload(Post.media))
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return PostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        author_id=post.author_id,
        district_id=post.district_id,
        media=[PostMediaOut.from_orm(m) for m in post.media],
        created_at=post.created_at,
        updated_at=post.updated_at,
        like_count=len(post.votes)
    )


# Create comment
@router.post("/{post_id}/comments", response_model=CommentResponse)
async def create_comment(
    post_id: int,
    comment: CommentCreate,
    current_user: User = Depends(lambda: None),  
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Post).where(Post.id == post_id)
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if comment.parent_id:
        parent_stmt = select(Comment).where(Comment.id == comment.parent_id)
        parent_result = await db.execute(parent_stmt)
        parent = parent_result.scalar_one_or_none()
        if not parent or parent.post_id != post_id:
            raise HTTPException(status_code=404, detail="Parent comment not found")

    db_comment = Comment(content=comment.content, author_id=current_user.id, post_id=post_id, parent_id=comment.parent_id)
    db.add(db_comment)
    await db.commit()
    await db.refresh(db_comment)
    return db_comment


# List comments
@router.get("/{post_id}/comments", response_model=List[CommentResponse])
async def list_comments(post_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Comment).where(Comment.post_id == post_id).options(selectinload(Comment.replies))
    result = await db.execute(stmt)
    return result.scalars().unique().all()


@router.post("/{post_id}/like")
async def like_post(
    post_id: int,
    current_user: User = Depends(get_current_user),  # <-- use real auth
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Post).where(Post.id == post_id)
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    vote_stmt = select(Vote).where(Vote.user_id == current_user.id, Vote.post_id == post_id)
    existing_vote = (await db.execute(vote_stmt)).scalar_one_or_none()
    if existing_vote:
        raise HTTPException(status_code=400, detail="Already liked")
    
    db_vote = Vote(user_id=current_user.id, post_id=post_id, vote_type="like")
    db.add(db_vote)
    await db.commit()
    return {"message": "Post liked"}


# Live feeds
@router.post("/live", response_model=LiveFeedResponse)
async def create_live_feed(live_feed: LiveFeedCreate, current_user: User = Depends(lambda: None), db: AsyncSession = Depends(get_db)):
    if current_user.role != "journalist":
        raise HTTPException(status_code=403, detail="Only journalists can create live feeds")
    db_feed = LiveFeed(content=live_feed.content, journalist_id=current_user.id, district_id=live_feed.district_id)
    db.add(db_feed)
    await db.commit()
    await db.refresh(db_feed)
    return db_feed

@router.get("/live", response_model=List[LiveFeedResponse])
async def list_live_feeds(skip: int = 0, limit: int = 10, district_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    stmt = select(LiveFeed)
    if district_id:
        stmt = stmt.filter(LiveFeed.district_id == district_id)
    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().unique().all()
