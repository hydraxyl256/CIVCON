from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models import Comment, Post, User
from app.schemas import CommentCreate, CommentResponse

router = APIRouter(prefix="/posts", tags=["Comments"])


#  Create comment
@router.post("/{post_id}/comments", response_model=CommentResponse)
async def create_comment(
    post_id: int,
    payload: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    post = await db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    db_comment = Comment(
        content=payload.content.strip(),
        author_id=current_user.id,
        post_id=post_id,
        parent_id=payload.parent_id,
        created_at=datetime.now(UTC),
    )
    db.add(db_comment)
    await db.commit()
    await db.refresh(db_comment)
    return db_comment


#  Get all comments for a post
@router.get("/{post_id}/comments", response_model=list[CommentResponse])
async def get_comments(
    post_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch all comments (and nested replies) for a post.
    """
    stmt = (
        select(Comment)
        .where(Comment.post_id == post_id, Comment.parent_id.is_(None))
        .options(
            selectinload(Comment.author),
            selectinload(Comment.replies).selectinload(Comment.author)
        )
    )
    result = await db.execute(stmt)
    comments = result.scalars().unique().all()
    return comments
