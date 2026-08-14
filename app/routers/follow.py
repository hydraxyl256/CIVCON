
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models import Follower, User
from app.schemas import UserPublic

router = APIRouter(prefix="/follow", tags=["Follow"])


#  FOLLOW USER
@router.post("/{user_id}", status_code=201)
async def follow_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Follow another user."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself")

    # Check if target user exists
    target = await db.execute(select(User).where(User.id == user_id))
    target_user = target.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already following
    existing = await db.execute(
        select(Follower).where(
            Follower.follower_id == current_user.id,
            Follower.followed_id == user_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Already following this user")

    db.add(Follower(follower_id=current_user.id, followed_id=user_id))
    await db.commit()

    return {"message": f"You are now following {target_user.first_name}"}



#  UNFOLLOW USER
@router.delete("/{user_id}", status_code=200)
async def unfollow_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Unfollow a user."""
    result = await db.execute(
        select(Follower).where(
            Follower.follower_id == current_user.id,
            Follower.followed_id == user_id,
        )
    )
    follow = result.scalar_one_or_none()
    if not follow:
        raise HTTPException(status_code=404, detail="Not following this user")

    await db.delete(follow)
    await db.commit()
    return {"message": "Unfollowed successfully"}



#  GET FOLLOWERS
@router.get("/{user_id}/followers", response_model=list[UserPublic])
async def get_followers(user_id: int, db: AsyncSession = Depends(get_db)):
    """Get list of followers for a given user."""
    result = await db.execute(
        select(User)
        .join(Follower, Follower.follower_id == User.id)
        .where(Follower.followed_id == user_id)
    )
    return result.scalars().all()



#  GET FOLLOWING
@router.get("/{user_id}/following", response_model=list[UserPublic])
async def get_following(user_id: int, db: AsyncSession = Depends(get_db)):
    """Get list of users that the given user is following."""
    result = await db.execute(
        select(User)
        .join(Follower, Follower.followed_id == User.id)
        .where(Follower.follower_id == user_id)
    )
    return result.scalars().all()



#  MUTUAL FOLLOWERS
@router.get("/{user_id}/mutual-followers", response_model=list[UserPublic])
async def get_mutual_followers(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get mutual followers between the current user and another user."""
    if user_id == current_user.id:
        return []

    # Followers that current_user follows
    current_stmt = select(Follower.followed_id).where(
        Follower.follower_id == current_user.id
    )
    current_res = await db.execute(current_stmt)
    current_following = [r[0] for r in current_res.fetchall()]

    # Followers that target user follows
    target_stmt = select(Follower.followed_id).where(
        Follower.follower_id == user_id
    )
    target_res = await db.execute(target_stmt)
    target_following = [r[0] for r in target_res.fetchall()]

    # Find intersection (mutuals)
    mutual_ids = list(set(current_following) & set(target_following))
    if not mutual_ids:
        return []

    users_stmt = select(User).where(User.id.in_(mutual_ids))
    users_res = await db.execute(users_stmt)
    return users_res.scalars().all()



#  FOLLOW COUNTS
@router.get("/{user_id}/counts")
async def get_follow_counts(user_id: int, db: AsyncSession = Depends(get_db)):
    """Get follower and following counts for a given user."""
    followers_count = await db.scalar(
        select(func.count()).select_from(Follower).where(Follower.followed_id == user_id)
    )
    following_count = await db.scalar(
        select(func.count()).select_from(Follower).where(Follower.follower_id == user_id)
    )

    return {
        "followers": followers_count or 0,
        "following": following_count or 0,
    }
