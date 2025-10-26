from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from app.database import get_db
from app.models import User, Post, Comment, Vote, LiveFeed, PostMedia, Notification
from app.schemas import (
    PostResponse,
    PostCreate,
    CommentResponse,
    CommentCreate,
    LiveFeedResponse,
    LiveFeedCreate,
    PostMediaOut,
    UserPublic
)
import logging
import cloudinary
import cloudinary.uploader
from app.config import settings
from sqlalchemy import func
from app.utils.social_share import share_to_social_media, send_inbox_message
from .oauth2 import get_current_user
from datetime import datetime

router = APIRouter(prefix="/posts", tags=["Posts"])
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Configure Cloudinary
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
)


# CREATE POST
@router.post("/", response_model=PostResponse)
async def create_post(
    title: str = Form(...),
    content: str = Form(...),
    district_id: Optional[str] = Form(None),
    media_files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    post = Post(
        title=title,
        content=content,
        author_id=current_user.id,
        district_id=district_id,
    )

    db.add(post)
    await db.commit()
    await db.refresh(post)

    media_list = []
    if media_files:
        for file in media_files:
            upload_result = cloudinary.uploader.upload(
                file.file, folder="civcon/posts", resource_type="auto"
            )
            media = PostMedia(
                post_id=post.id,
                media_url=upload_result["secure_url"],
                media_type=file.content_type,
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
        author=UserPublic.from_orm(current_user),
        district_id=post.district_id,
        created_at=post.created_at,
        updated_at=post.updated_at,
        like_count=0,
        comments=[],
        share_count=getattr(post, "share_count", 0),
    )



# GET SINGLE POST
@router.get("/{post_id}", response_model=PostResponse)
async def get_post(post_id: int, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(Post)
        .where(Post.id == post_id)
        .options(
            selectinload(Post.author),
            selectinload(Post.media),
            selectinload(Post.votes),
            selectinload(Post.comments).selectinload(Comment.author),
        )
    )
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    return PostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        author=UserPublic.from_orm(post.author),
        district_id=post.district_id,
        media=[PostMediaOut.from_orm(m) for m in post.media],
        created_at=post.created_at,
        updated_at=post.updated_at,
        like_count=len(post.votes or []),
        comments=[
            CommentResponse.from_orm(c)
            for c in post.comments if c is not None
        ],
        share_count=getattr(post, "share_count", 0),
    )



# CREATE COMMENT
@router.post("/{post_id}/comments", response_model=CommentResponse)
async def create_comment(
    post_id: int,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    content = payload.get("content")
    parent_id = payload.get("parent_id")

    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="Comment content is required.")

    post = (await db.execute(select(Post).where(Post.id == post_id))).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if parent_id:
        parent_comment = (
            await db.execute(select(Comment).where(Comment.id == parent_id))
        ).scalar_one_or_none()
        if not parent_comment:
            raise HTTPException(status_code=404, detail="Parent comment not found")

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

    return CommentResponse(
        id=db_comment.id,
        content=db_comment.content,
        author=UserPublic.from_orm(db_comment.author),
        parent_id=db_comment.parent_id,
        created_at=db_comment.created_at,
        updated_at=db_comment.updated_at,
        replies=[],
    )



# LIST POSTS
@router.get("/", response_model=List[PostResponse])
async def list_posts(
    skip: int = 0,
    limit: int = 10,
    district_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Post)
        .options(
            selectinload(Post.author),
            selectinload(Post.media),
            selectinload(Post.votes),
            selectinload(Post.comments).selectinload(Comment.author),
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
        return {
            "id": comment.id,
            "content": comment.content,
            "created_at": comment.created_at,
            "updated_at": comment.updated_at,
            "parent_id": comment.parent_id,
            "author": UserPublic.from_orm(comment.author).model_dump()
            if comment.author else None,
            "replies": [
                serialize_comment(reply) for reply in (comment.replies or [])
            ],
        }

    serialized_posts = []
    for p in posts:
        serialized_posts.append({
            "id": p.id,
            "title": p.title,
            "content": p.content,
            "author": UserPublic.from_orm(p.author).model_dump() if p.author else None,
            "district_id": p.district_id,
            "media": [PostMediaOut.from_orm(m).model_dump() for m in p.media],
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            "like_count": len(p.votes or []),
            "comments": [serialize_comment(c) for c in (p.comments or [])],
            "share_count": getattr(p, "share_count", 0),
        })

    return serialized_posts



# LIKE POST
@router.post("/{post_id}/like")
async def like_post(
    post_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
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



# CREATE LIVE FEED
@router.post("/live", response_model=LiveFeedResponse)
async def create_live_feed(
    live_feed: LiveFeedCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role != "journalist":
        raise HTTPException(status_code=403, detail="Only journalists can create live feeds")

    db_feed = LiveFeed(
        content=live_feed.content,
        journalist_id=current_user.id,
        district_id=live_feed.district_id,
    )

    db.add(db_feed)
    await db.commit()
    await db.refresh(db_feed)
    return db_feed



# LIST LIVE FEEDS
@router.get("/live", response_model=List[LiveFeedResponse])
async def list_live_feeds(
    skip: int = 0,
    limit: int = 10,
    district_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(LiveFeed)
    if district_id:
        stmt = stmt.filter(LiveFeed.district_id == district_id)
    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().unique().all()



# SHARE POST
@router.post("/{post_id}/share", response_model=PostResponse)
async def share_post(
    post_id: int,
    share_to: Optional[str] = None,  # "facebook", "twitter", "inbox"
    message: Optional[str] = None,   # optional message
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Post)
        .where(Post.id == post_id)
        .options(
            selectinload(Post.author),
            selectinload(Post.comments),
            selectinload(Post.media),
            selectinload(Post.votes),
        )
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    post.share_count = (getattr(post, "share_count", 0) or 0) + 1
    post.updated_at = datetime.utcnow()

    try:
        db.add(post)
        await db.commit()
        await db.refresh(post)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update share count: {e}")

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
            logger.warning(f"Notification creation failed: {e}")

    try:
        if share_to == "facebook":
            await share_to_social_media("facebook", post, current_user)
        elif share_to == "twitter":
            await share_to_social_media("twitter", post, current_user)
        elif share_to == "inbox":
            await send_inbox_message(post, current_user, message)
    except Exception as e:
        logger.warning(f"External share failed: {e}")

    return PostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        author=UserPublic.from_orm(post.author),
        district_id=post.district_id,
        media=[PostMediaOut.from_orm(m) for m in post.media],
        created_at=post.created_at,
        updated_at=post.updated_at,
        like_count=len(post.votes or []),
        comments=[CommentResponse.from_orm(c) for c in post.comments],
        share_count=post.share_count,
    )
