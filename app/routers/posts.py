import asyncio
import logging
from datetime import UTC, datetime

import cloudinary
import cloudinary.uploader
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.db_helpers import batched_counts
from app.dependencies.auth import get_current_user
from app.models import Comment, LiveFeed, Notification, Post, PostMedia, User, Vote
from app.schemas import (
    CommentResponse,
    LiveFeedCreate,
    LiveFeedResponse,
    PostMediaOut,
    PostResponse,
    UserPublic,
)
from app.utils.social_share import send_inbox_message, share_to_social_media

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
    district_id: str | None = Form(None),
    media_files: list[UploadFile] | None = File(None),
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
        # Perf: Cloudinary's `upload` is a blocking SDK call that can
        # take seconds. Run all uploads concurrently in a thread pool
        # via asyncio.to_thread, and use asyncio.gather to fan them
        # out in parallel. We deliberately keep the existing
        # two-commit pattern: the post is already committed above
        # (so a media upload failure today does NOT roll back the
        # post, and this change preserves that contract).
        async def _upload_one(file: UploadFile):
            return await asyncio.to_thread(
                cloudinary.uploader.upload,
                file.file, folder="civcon/posts", resource_type="auto",
            ), file.content_type

        upload_results = await asyncio.gather(*(_upload_one(f) for f in media_files))
        for upload_result, content_type in upload_results:
            media = PostMedia(
                post_id=post.id,
                media_url=upload_result["secure_url"],
                media_type=content_type,
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
    # SECURITY (F-006): filter out soft-deleted posts. Read paths
    # must never serve a deleted row to anyone except moderators
    # (who have their own admin endpoints).
    stmt = (
        select(Post)
        .where(Post.id == post_id)
        .where(Post.deleted_at.is_(None))
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


# LIST POSTS
# `response_model_exclude_none=True` strips null fields from each
# Post in the list. The feed already handles "field absent" the
# same as "field null" — saves ~25% on a 10-post list response.
@router.get(
    "/",
    response_model=list[PostResponse],
    response_model_exclude_none=True,
)
async def list_posts(
    skip: int = 0,
    limit: int = 10,
    district_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Post)
        .options(
            selectinload(Post.author),
            selectinload(Post.media),
            # Perf: was `selectinload(Post.votes)` only to support
            # `len(p.votes)` below — replaced with a batched count
            # query so we don't materialise every vote row.
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

    # Perf: batched like counts instead of len(p.votes). One GROUP BY
    # query for the whole page instead of one eager-load per post.
    like_counts = await batched_counts(
        db, model=Vote, fk_col=Vote.post_id, ids=[p.id for p in posts],
    )

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
            "like_count": like_counts.get(p.id, 0),
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
    # SECURITY (F-006): do not allow likes/shares on soft-deleted posts.
    stmt = select(Post).where(Post.id == post_id).where(Post.deleted_at.is_(None))
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    vote_stmt = select(Vote).where(Vote.user_id == current_user.id, Vote.post_id == post_id)
    existing_vote = (await db.execute(vote_stmt)).scalar_one_or_none()

    if existing_vote:
        # Unlike: remove the vote row and decrement the post's denormalised
        # like counter in a single UPDATE ... RETURNING round-trip, so the
        # response carries the new count without a separate COUNT(*) query.
        await db.delete(existing_vote)
        new_count = await db.scalar(
            update(Post)
            .where(Post.id == post_id, Post.like_count > 0)
            .values(like_count=Post.like_count - 1)
            .returning(Post.like_count)
        )
        if new_count is None:
            # Counter was already 0 (drift); fall back to a COUNT(*).
            await db.commit()
            new_count = await db.scalar(
                select(func.count()).select_from(Vote).where(Vote.post_id == post_id)
            ) or 0
        else:
            await db.commit()
        return {"liked": False, "like_count": int(new_count)}

    # Like: insert the vote row and increment the counter in the same
    # round-trip, returning the new count.
    db_vote = Vote(user_id=current_user.id, post_id=post_id, vote_type="like")
    db.add(db_vote)
    new_count = await db.scalar(
        update(Post)
        .where(Post.id == post_id)
        .values(like_count=Post.like_count + 1)
        .returning(Post.like_count)
    )
    if new_count is None:
        # Post vanished between SELECT and UPDATE (race); fall back.
        await db.commit()
        new_count = await db.scalar(
            select(func.count()).select_from(Vote).where(Vote.post_id == post_id)
        ) or 0
    else:
        await db.commit()
    return {"liked": True, "like_count": int(new_count)}



# CREATE LIVE FEED
@router.post("/live", response_model=LiveFeedResponse)
async def create_live_feed(
    live_feed: LiveFeedCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role.value != "journalist":
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
@router.get("/live", response_model=list[LiveFeedResponse])
async def list_live_feeds(
    skip: int = 0,
    limit: int = 10,
    district_id: str | None = None,
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
    share_to: str | None = None,  # "facebook", "twitter", "inbox"
    message: str | None = None,   # optional message
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # SECURITY (F-006): filter soft-deleted posts out of share path too.
    result = await db.execute(
        select(Post)
        .where(Post.id == post_id)
        .where(Post.deleted_at.is_(None))
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
    post.updated_at = datetime.now(UTC)

    try:
        db.add(post)
        await db.commit()
        await db.refresh(post)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update share count: {e}") from e

    if post.author_id != current_user.id:
        notification = Notification(
            user_id=post.author_id,
            message=f"{current_user.first_name} shared your post.",
            post_id=post.id,
            created_at=datetime.now(UTC),
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


# DELETE POST (soft-delete)
# SECURITY (F-006): the previous version of the SPA called
# `api.delete(\`/posts/${postId}\`)`, but no backend handler existed.
# The handler is wired here with explicit authorisation:
#   - The author can delete their own post.
#   - Admins can delete any post (moderation use case).
#   - Anyone else gets a 403.
# We use a soft-delete (set `deleted_at`) so the row remains for
# moderation audit / undelete. Read paths must filter `deleted_at IS NULL`.
@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Already deleted (idempotent). Treat as success.
    if post.deleted_at is not None:
        return

    role_value = (getattr(current_user.role, "value", None) or "").lower()
    is_author = post.author_id == current_user.id
    is_admin = role_value == "admin"
    if not (is_author or is_admin):
        raise HTTPException(
            status_code=403,
            detail="You can only delete your own posts.",
        )

    post.deleted_at = datetime.now(UTC)
    post.deleted_by_id = current_user.id
    post.updated_at = datetime.now(UTC)
    await db.commit()
    logger.info(
        "post.soft_deleted",
        extra={
            "post_id": post.id,
            "author_id": post.author_id,
            "deleted_by_id": current_user.id,
            "deleted_by_role": role_value,
        },
    )
    return
