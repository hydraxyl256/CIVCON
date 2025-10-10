from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from .. import models, schemas
from ..database import get_db
from app.schemas import Vote, VoteResponse
from ..services.notifications import create_and_send_notification
from ..routers.users import get_current_user

router = APIRouter(
    prefix="/votes",
    tags=["Votes/Likes"]
)

@router.post("/", status_code=status.HTTP_201_CREATED)
async def vote_post(
    vote: schemas.Vote,
    db: AsyncSession = Depends(get_db),
    current_user: schemas.UserOut = Depends(get_current_user)
):
    # Validate vote direction
    if vote.dir not in [0, 1]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Vote direction must be 0 (unvote) or 1 (upvote)"
        )

    # Fetch the post
    post_result = await db.execute(select(models.Post).where(models.Post.id == vote.post_id))
    post = post_result.scalar_one_or_none()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post with id {vote.post_id} does not exist"
        )

    # Check if user already voted
    vote_result = await db.execute(
        select(models.Vote).where(
            models.Vote.post_id == vote.post_id,
            models.Vote.user_id == current_user.id
        )
    )
    found_vote = vote_result.scalar_one_or_none()

    # Handle vote/unvote
    if vote.dir == 1:
        if found_vote:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"You have already voted on post {vote.post_id}"
            )
        new_vote = models.Vote(post_id=vote.post_id, user_id=current_user.id, vote_type="like")
        db.add(new_vote)
        await db.commit()
        await db.refresh(new_vote)

        # Send notification to post author if not self-vote
        if post.author_id != current_user.id:
            await create_and_send_notification(
                db=db,
                user_id=post.author_id,
                type="VOTE",
                message=f"{current_user.first_name} liked your post",
                post_id=post.id
            )

        # Count likes
        like_query = select(func.count()).select_from(models.Vote).where(models.Vote.post_id == vote)
