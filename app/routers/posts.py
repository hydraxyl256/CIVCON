from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from app.database import get_db
from app.models import User, Post, Comment, Vote, LiveFeed, PostMedia, Notification
from app.schemas import PostResponse, PostCreate, CommentResponse, CommentCreate, LiveFeedResponse, LiveFeedCreate, PostMediaOut, UserBase
import logging
import os
from .oauth2 import get_current_user
from datetime import datetime
import cloudinary
import cloudinary.uploader
from app.config import settings
from sqlalchemy import func
from app.utils.social_share import share_to_social_media, send_inbox_message


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


# List Posts with Comments (with pre-fetching)
@router.get("/", response_model=List[PostResponse])
async def list_posts(
    skip: int = 0,
    limit: int = 10,
    district_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Return all posts with author, media, likes, and nested comments.
    """

    stmt = (
        select(Post)
        .options(
            selectinload(Post.author),
            selectinload(Post.media),
            selectinload(Post.votes),
            selectinload(Post.comments)
                .selectinload(Comment.author),
            selectinload(Post.comments)
                .selectinload(Comment.replies)
                .selectinload(Comment.author),
        )
        .offset(skip)
        .limit(limit)
    )

    if district_id:
        stmt = stmt.filter(Post.district_id == district_id)

    result = await db.execute(stmt)
    posts = result.scalars().unique().all()

    def serialize_comment(comment: Comment):
        """Manual recursive serialization to avoid lazy I/O"""
        return {
            "id": comment.id,
            "content": comment.content,
            "created_at": comment.created_at,
            "updated_at": comment.updated_at,
            "parent_id": comment.parent_id,
            "author": {
                "id": comment.author.id,
                "first_name": comment.author.first_name,
                "last_name": comment.author.last_name,
                "username": comment.author.username,
                "profile_image": comment.author.profile_image,
            } if comment.author else None,
            "replies": [
                serialize_comment(reply)
                for reply in (comment.replies or [])
            ],
        }

    serialized_posts = []
    for p in posts:
        serialized_posts.append({
            "id": p.id,
            "title": p.title,
            "content": p.content,
            "author": {
                "id": p.author.id,
                "first_name": p.author.first_name,
                "last_name": p.author.last_name,
                "username": p.author.username,
                "profile_image": p.author.profile_image,
            } if p.author else None,
            "district_id": p.district_id,
            "media": [PostMediaOut.from_orm(m) for m in p.media],
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            "like_count": len(p.votes or []),
            "comments": [serialize_comment(c) for c in (p.comments or [])],
        })

    return serialized_posts


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


@router.post("/{post_id}/share", response_model=PostResponse)
async def share_post(
    post_id: int,
    share_to: str | None = None,  # "facebook", "twitter", "inbox"
    message: str | None = None,   # custom message to include
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
     Share post to other users or social media
    - Increments share_count
    - Optionally sends notification or social post
    - Returns full updated PostResponse
    """

    #  1. Fetch post
    result = await db.execute(
        select(Post)
        .where(Post.id == post_id)
        .options(
            selectinload(Post.author),
            selectinload(Post.comments),
            selectinload(Post.media),
            selectinload(Post.votes)
        )
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    #  2. Increment share count
    post.share_count = (getattr(post, "share_count", 0) or 0) + 1
    post.updated_at = datetime.utcnow()

    try:
        db.add(post)
        await db.commit()
        await db.refresh(post)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update share count: {e}")

    #  3. Optional: send a notification to the author
    if post.author_id != current_user.id:
        notification = Notification(
            user_id=post.author_id,
            message=f"{current_user.first_name} shared your post.",
            post_id=post.id,
            created_at=datetime.utcnow(),
        )
        db.add(notification)
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"Notification creation failed: {e}")

    #  4. Optional: share externally (social media / inbox)
    try:
        if share_to == "facebook":
            await share_to_social_media("facebook", post, current_user)
        elif share_to == "twitter":
            await share_to_social_media("twitter", post, current_user)
        elif share_to == "inbox":
            await send_inbox_message(post, current_user, message)
    except Exception as e:
        print(f"External share failed: {e}")

    #  Return full PostResponse
    return PostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        author=post.author,
        district_id=post.district_id,
        media=[PostMediaOut.from_orm(m) for m in post.media],
        created_at=post.created_at,
        updated_at=post.updated_at,
        like_count=len(post.votes),
        comments=[CommentResponse.from_orm(c) for c in post.comments],
        share_count=post.share_count,
    )