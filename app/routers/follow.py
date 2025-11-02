from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models import Follower, User
from app.routers.oauth2 import get_current_user

router = APIRouter(prefix="/follow", tags=["follow"])

@router.post("/{user_id}", status_code=201)
async def follow_user(user_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Follow another user."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself")

    result = await db.execute(
        select(Follower).where(
            Follower.follower_id == current_user.id,
            Follower.followed_id == user_id
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Already following this user")

    follow = Follower(follower_id=current_user.id, followed_id=user_id)
    db.add(follow)
    await db.commit()
    return {"message": "Now following user"}

@router.delete("/{user_id}", status_code=200)
async def unfollow_user(user_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Unfollow a user."""
    result = await db.execute(
        select(Follower).where(
            Follower.follower_id == current_user.id,
            Follower.followed_id == user_id
        )
    )
    follow = result.scalar_one_or_none()
    if not follow:
        raise HTTPException(status_code=404, detail="Not following this user")

    await db.delete(follow)
    await db.commit()
    return {"message": "Unfollowed user"}

@router.get("/{user_id}/followers")
async def get_followers(user_id: int, db: AsyncSession = Depends(get_db)):
    """Get list of followers for a given user."""
    result = await db.execute(
        select(User).join(Follower, Follower.follower_id == User.id).where(Follower.followed_id == user_id)
    )
    followers = result.scalars().all()
    return followers

@router.get("/{user_id}/following")
async def get_following(user_id: int, db: AsyncSession = Depends(get_db)):
    """Get list of users that the given user is following."""
    result = await db.execute(
        select(User).join(Follower, Follower.followed_id == User.id).where(Follower.follower_id == user_id)
    )
    following = result.scalars().all()
    return following
