from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from app.database import get_db
from app.models import User, Post, Comment, Vote, LiveFeed, PostMedia, Notification
from app.schemas import PostResponse, PostCreate, CommentResponse, CommentCreate, LiveFeedResponse, LiveFeedCreate, PostMediaOut
import logging
import os
from .oauth2 import get_current_user
from datetime import datetime
import cloudinary
import cloudinary.uploader
from app.config import settings
from sqlalchemy import func


router = APIRouter(prefix="/posts", tags=["Posts"])
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# Configure Cloudinary
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
)


# Create Post with Media Uploads
@router.post("/", response_model=PostResponse)
async def create_post(
    title: str = Form(...),
    content: str = Form(...),
    district_id: Optional[str] = Form(None),
    media_files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    #  Create Post
    post = Post(
        title=title,
        content=content,
        author_id=current_user.id,
        district_id=district_id
    )

    db.add(post)
    await db.commit()
    await db.refresh(post)

    #  Handle Media Uploads
    media_list = []
    if media_files:
        for file in media_files:
            upload_result = cloudinary.uploader.upload(
                file.file,
                folder="civcon/posts",
                resource_type="auto"  # supports image/video
            )

            media = PostMedia(
                post_id=post.id,
                media_url=upload_result["secure_url"],
                media_type=file.content_type
            )
            db.add(media)
            media_list.append(media)
        await db.commit()

    await db.refresh(post)

    #   Return Pydantic-compliant response
    return PostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        media=[PostMediaOut.from_orm(m) for m in media_list],
        author=current_user,  
        district_id=post.district_id,
        created_at=post.created_at,
        updated_at=post.updated_at,
        like_count=0,
        comments=[],
        share_count=getattr(post, "share_count", 0)
    )


# List posts with like count
@router.get("/", response_model=List[PostResponse])
async def list_posts(
    skip: int = 0,
    limit: int = 10,
    district_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve posts with author, media, and like counts.
    """
    try:
        stmt = (
            select(Post)
            .options(
                selectinload(Post.author),   
                selectinload(Post.media),    
                selectinload(Post.votes)     
            )
            .offset(skip)
            .limit(limit)
        )

        if district_id:
            stmt = stmt.filter(Post.district_id == district_id)

        result = await db.execute(stmt)
        posts = result.scalars().unique().all()

        if not posts:
            return []

        return [
            PostResponse(
                id=p.id,
                title=p.title,
                content=p.content,
                author=p.author,  
                district_id=p.district_id,
                media=[PostMediaOut.from_orm(m) for m in p.media],
                created_at=p.created_at,
                updated_at=p.updated_at,
                like_count=len(p.votes),
                share_count=getattr(p, "share_count", 0)
            )
            for p in posts
        ]

    except Exception as e:
        print("Error fetching posts:", str(e))
        raise HTTPException(status_code=500, detail="Internal server error")

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


#  List Comments for a Post
@router.get("/{post_id}/comments", response_model=List[CommentResponse])
async def list_comments(post_id: int, db: AsyncSession = Depends(get_db)):
    """
    Get all comments for a post (includes replies).
    """
    stmt = (
        select(Comment)
        .where(Comment.post_id == post_id)
        .options(selectinload(Comment.replies))
        .order_by(Comment.created_at.desc())
    )
    result = await db.execute(stmt)
    return result.scalars().unique().all()

# Like Endpoint
@router.post("/{post_id}/like")
async def like_post(
    post_id: int,
    current_user: User = Depends(get_current_user),
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
        await db.delete(existing_vote)
        await db.commit()
        count = await db.scalar(select(func.count()).select_from(Vote).where(Vote.post_id == post_id))
        return {"liked": False, "like_count": count or 0}

    db_vote = Vote(user_id=current_user.id, post_id=post_id, vote_type="like")
    db.add(db_vote)
    await db.commit()
    count = await db.scalar(select(func.count()).select_from(Vote).where(Vote.post_id == post_id))
    return {"liked": True, "like_count": count or 0}



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


# List live feeds
@router.get("/live", response_model=List[LiveFeedResponse])
async def list_live_feeds(skip: int = 0, limit: int = 10, district_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    stmt = select(LiveFeed)
    if district_id:
        stmt = stmt.filter(LiveFeed.district_id == district_id)
    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().unique().all()


#  Share Post (Return Full Updated Post)
@router.post("/{post_id}/share", response_model=PostResponse)
async def share_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Increment post share count, optionally notify author,
    and return the full updated Post object.
    """

    #  Ensure post exists
    result = await db.execute(
        select(Post)
        .where(Post.id == post_id)
        .options(
            selectinload(Post.author),
            selectinload(Post.comments),
            selectinload(Post.media)
        )
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    #  Increment share count safely
    post.share_count = (getattr(post, "share_count", 0) or 0) + 1
    post.updated_at = datetime.utcnow()

    try:
        db.add(post)
        await db.commit()
        await db.refresh(post)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update share count: {e}")

    #  Optional author notification
    if post.author_id != current_user.id:
        try:
            notification = Notification(
                user_id=post.author_id,
                message=f"{current_user.first_name} shared your post.",
                post_id=post.id,
                created_at=datetime.utcnow(),
            )
            db.add(notification)
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"Notification creation failed: {e}")

    #  Re-fetch post with relationships (fresh data)
    refreshed_post = await db.execute(
        select(Post)
        .where(Post.id == post_id)
        .options(
            selectinload(Post.author),
            selectinload(Post.comments),
            selectinload(Post.media)
        )
    )
    updated_post = refreshed_post.scalar_one_or_none()

    return updated_post