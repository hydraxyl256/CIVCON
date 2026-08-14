from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database import get_db
from app.db_helpers import batched_counts
from app.dependencies.auth import get_current_user
from app.models import Comment, Notification, Post, User, Vote
from app.routers.permissions import require_admin
from app.schemas import PostResponse

router = APIRouter(
    prefix="/admin/moderation",
    tags=["Admin Moderation"],
    dependencies=[Depends(require_admin)],
)


#  Backwards-compatible alias — endpoints that still depend on `admin_required`
#  will continue to work and inherit the same require_admin check.
admin_required = require_admin



#  GET /admin/moderation/posts
# Fetch all posts with status, reports, engagement, etc.
@router.get("/posts")
async def get_all_posts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
) -> list[dict[str, Any]]:
    stmt = (
        select(Post, User)
        .join(User, Post.author_id == User.id)
        .order_by(desc(Post.created_at))
    )
    result = await db.execute(stmt)
    records = result.all()

    posts = []
    # Perf: batched like + comment counts via two GROUP BY queries
    # instead of 2*N scalar queries. JSON shape unchanged.
    post_ids = [post.id for post, _author in records]
    like_counts = await batched_counts(
        db, model=Vote, fk_col=Vote.post_id, ids=post_ids,
    )
    comment_counts = await batched_counts(
        db, model=Comment, fk_col=Comment.post_id, ids=post_ids,
    )

    for post, author in records:
        like_count = like_counts.get(post.id, 0)
        comment_count = comment_counts.get(post.id, 0)
        reports = getattr(post, "report_count", 0) or 0

        posts.append(
            {
                "id": post.id,
                "content": post.content,
                "author": f"{author.first_name} {author.last_name}" if author else "Unknown",
                "authorId": author.id if author else None,
                "status": post.status or "Approved",
                "createdAt": post.created_at,
                "views": getattr(post, "views", 0),
                "likes": like_count,
                "comments": comment_count,
                "reports": reports,
            }
        )

    return posts



#  POST /admin/moderation/post/{id}/approve
# Approve a flagged or pending post + notify author
@router.post("/post/{post_id}/approve")
async def approve_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
):
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Update post status
    post.status = "Approved"
    post.updated_at = datetime.now(UTC)

    # Create notification for author
    notification = Notification(
        user_id=post.author_id,
        message=f"✅ Your post '{post.content[:50]}...' has been approved by moderators.",
        post_id=post.id,
        created_at=datetime.now(UTC),
    )
    db.add(notification)

    await db.commit()
    await db.refresh(post)

    return {"message": f"Post {post.id} approved successfully and author notified."}



#  POST /admin/moderation/post/{id}/remove
# Remove a post (soft delete) + notify author
@router.post("/post/{post_id}/remove")
async def remove_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
):
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Update post status
    post.status = "Removed"
    post.updated_at = datetime.now(UTC)

    # Create notification for author
    notification = Notification(
        user_id=post.author_id,
        message=f" Your post '{post.content[:50]}...' was removed by moderators for review.",
        post_id=post.id,
        created_at=datetime.now(UTC),
    )
    db.add(notification)

    await db.commit()
    await db.refresh(post)

    return {"message": f"Post {post.id} removed successfully and author notified."}



#  GET /admin/moderation/topics
# Fetch trending topics & engagement based on post keywords
@router.get("/topics")
async def get_trending_topics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_required),
):
    stmt = select(Post.content)
    result = await db.execute(stmt)
    posts = result.scalars().all()

    if not posts:
        return []

    # Keyword-based topic extraction
    keywords = ["education", "health", "climate", "election", "youth", "governance"]

    topics: dict[str, dict[str, Any]] = {}
    for keyword in keywords:
        related_posts = [p for p in posts if keyword.lower() in p.lower()]
        topics[keyword.capitalize()] = {
            "id": hash(keyword) % 10000,
            "name": keyword.capitalize(),
            "posts": len(related_posts),
            "engagement": min(len(related_posts) * 8, 100),
            "trend": "up" if len(related_posts) > 3 else "stable",
            "status": "Active" if len(related_posts) > 0 else "Suspended",
        }

    return list(topics.values())


#  Suspend a post
@router.post("/post/{post_id}/suspend", response_model=PostResponse)
async def suspend_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Ensure admin only
    if current_user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if getattr(post, "status", None) == "Suspended":
        raise HTTPException(status_code=400, detail="Post already suspended")

    post.status = "Suspended"
    post.updated_at = datetime.now(UTC)

    db.add(post)
    await db.commit()
    await db.refresh(post)

    return post