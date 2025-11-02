from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from pydantic import BaseModel, EmailStr
from app.database import get_db
from app.models import User, MP, Role
from app.crud import get_user_by_email
from app.config import settings
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import os
import uuid
import logging
from datetime import datetime
import json
from sqlalchemy.orm import selectinload
from app.routers.oauth2 import get_current_user
from app.routers.auth import upload_to_cloudinary
from app.schemas import  UserResponse, UserUpdate
import cloudinary.uploader
from app import models, schemas
from sqlalchemy import func
from app.schemas import UserOut, MutualInterestsResponse
from app.models import Post, Comment, Vote
from sqlalchemy import delete
from sqlalchemy.orm import relationship
from app.models import Follower
from typing import List


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm

router = APIRouter(prefix="/users", tags=["users"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

followers = relationship(
    "Follower",
    foreign_keys="Follower.followed_id",
    back_populates="followed",
    cascade="all, delete-orphan"
)
following = relationship(
    "Follower",
    foreign_keys="Follower.follower_id",
    back_populates="follower",
    cascade="all, delete-orphan"
)

# Get logged-in user's profile
@router.get("/me", response_model=UserResponse)
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Explicitly load any lazy-loaded relationships like 'notifications'
    result = await db.execute(
        select(User)
        .options(selectinload(User.notifications))
        .where(User.id == current_user.id)
    )
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# Get public user profile by ID
@router.get("/{user_id}", response_model=schemas.UserPublic)
async def get_public_user_profile(user_id: int, db: AsyncSession = Depends(get_db)):
    """
     Fetch a public user's profile by ID.
    Does not expose sensitive fields like email or password.
    """
    stmt = select(models.User).where(models.User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user

# Get user by username
@router.get("/by-username/{username}", response_model=schemas.UserPublic)
async def get_user_by_username(username: str, db: AsyncSession = Depends(get_db)):
    """
     Get a user's public profile by username (case-insensitive).
    - Returns only public fields.
    - Safe for public display.
    """
    stmt = (
        select(models.User)
        .where(func.lower(models.User.username) == func.lower(username))
        .limit(1)
    )

    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user


# Update logged-in user's profile
@router.put("/profile", response_model=UserOut)
async def update_user_profile(
    first_name: str = Form(None),
    last_name: str = Form(None),
    occupation: str = Form(None),
    bio: str = Form(None),
    region: str = Form(None),
    district_id: str = Form(None),
    privacy_level: str = Form(None),
    profile_image: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update logged-in user's profile details."""
    try:
        result = await db.execute(select(User).where(User.id == current_user.id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Update editable fields
        if first_name: user.first_name = first_name
        if last_name: user.last_name = last_name
        if occupation: user.occupation = occupation
        if bio: user.bio = bio
        if region: user.region = region
        if district_id: user.district_id = district_id
        if privacy_level: user.privacy_level = privacy_level

        # Cloudinary upload
        if profile_image:
            try:
                upload_result = await upload_to_cloudinary(profile_image, folder="civcon/profiles")
                user.profile_image = upload_result
            except Exception as e:
                logger.exception("Cloudinary upload failed")
                raise HTTPException(status_code=500, detail="Image upload failed")

        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info(f" Profile updated for {user.email}")

        return UserOut.model_validate(user)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error while updating profile")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")



# deactivate account endpoint
@router.patch("/deactivate", status_code=status.HTTP_200_OK)
async def deactivate_account(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Soft deactivate user account (keeps data but disables login)."""
    try:
        if not current_user.is_active:
            raise HTTPException(status_code=400, detail="Account already deactivated")

        current_user.is_active = False
        current_user.deactivated_at = datetime.utcnow()

        db.add(current_user)
        await db.commit()

        logger.info(f"🚫 Account deactivated for {current_user.email}")
        return {"message": "Account successfully deactivated. You can reactivate anytime by contacting support."}

    except Exception as e:
        logger.exception("Error deactivating account")
        raise HTTPException(status_code=500, detail="Internal server error")
    


# delete account endpoint
@router.delete("/delete", status_code=status.HTTP_200_OK)
async def delete_account(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Permanently delete a user and related data."""
    try:
        # Delete related posts, comments, likes (optional depending on models)
        await db.execute(delete(Vote).where(Vote.user_id == current_user.id))
        await db.execute(delete(Comment).where(Comment.user_id == current_user.id))
        await db.execute(delete(Post).where(Post.author_id == current_user.id))

        # Delete user record
        await db.execute(delete(User).where(User.id == current_user.id))
        await db.commit()

        logger.info(f" User {current_user.email} deleted successfully.")
        return {"message": "Your account and all data have been permanently deleted."}

    except Exception as e:
        logger.exception("Error deleting account")
        raise HTTPException(status_code=500, detail="Internal server error")



# List users with search and region filter
@router.get("/", response_model=list[schemas.UserPublic])
async def list_users(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    search: str = "",
    region: str = "",
):
    """
     Public endpoint to list users
    - Supports search (case-insensitive)
    - Supports region filter
    - Adds verified + real followers_count
    """
    from app.models import Follower  # import here to avoid circular imports

    stmt = select(models.User)

    if search:
        stmt = stmt.where(func.lower(models.User.username).like(f"%{search.lower()}%"))

    if region:
        stmt = stmt.where(models.User.region.ilike(region))

    stmt = stmt.order_by(models.User.id.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    users = result.scalars().all()

    verified_roles = {"leader", "politician", "journalist"}
    users_out = []

    for user in users:
        #  Verified
        is_verified = user.role and user.role.strip().lower() in verified_roles

        #  Real follower count
        followers_count = await db.scalar(
            select(func.count()).where(Follower.followed_id == user.id)
        )

        user_data = schemas.UserPublic.model_validate(user)
        user_data.verified = is_verified
        user_data.followers_count = followers_count or 0
        users_out.append(user_data)

    return users_out


# Get logged-in user's following list
@router.get("/me/following", response_model=list[schemas.UserPublic])
async def get_my_following(
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    from app.models import Follower
    stmt = (
        select(models.User)
        .join(Follower, Follower.followed_id == models.User.id)
        .where(Follower.follower_id == current_user.id)
    )
    result = await db.execute(stmt)
    return result.scalars().all()

@router.get("/suggested", response_model=list[schemas.UserPublic])
async def suggested_users(
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Suggest users that the current user might want to follow:
    - Prefer users from the same region.
    - Exclude those the user already follows.
    - Exclude self.
    """


    # Get IDs of users the current user already follows
    following_stmt = select(Follower.followed_id).where(Follower.follower_id == current_user.id)
    following_result = await db.execute(following_stmt)
    following_ids = [row[0] for row in following_result.fetchall()]

    # Main suggestion logic
    stmt = (
        select(models.User)
        .where(models.User.id != current_user.id)
        .where(~models.User.id.in_(following_ids))
        .order_by(func.random())
        .limit(10)
    )

    # Prioritize same region if possible
    if current_user.region:
        stmt = (
            select(models.User)
            .where(models.User.id != current_user.id)
            .where(models.User.region == current_user.region)
            .where(~models.User.id.in_(following_ids))
            .order_by(func.random())
            .limit(10)
        )

    result = await db.execute(stmt)
    users = result.scalars().all()
    return users

# Get mutual followers between current user and another user
@router.get("/{user_id}/mutual-followers", response_model=list[schemas.UserPublic])
async def get_mutual_followers(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
     Get mutual followers between the current user and another user.
    """
    from app.models import Follower

    if current_user.id == user_id:
        return []  # No mutuals with self

    # Followers of current user
    current_followers_stmt = select(Follower.followed_id).where(Follower.follower_id == current_user.id)
    current_following = [r[0] for r in (await db.execute(current_followers_stmt)).fetchall()]

    # Followers of the target user
    target_followers_stmt = select(Follower.followed_id).where(Follower.follower_id == user_id)
    target_following = [r[0] for r in (await db.execute(target_followers_stmt)).fetchall()]

    # Find intersection
    mutual_ids = list(set(current_following) & set(target_following))

    if not mutual_ids:
        return []

    result = await db.execute(select(models.User).where(models.User.id.in_(mutual_ids)))
    mutual_users = result.scalars().all()

    return mutual_users


# Get Mutual Interest
@router.get("/{user_id}/mutual-interests", response_model=MutualInterestsResponse)
async def get_mutual_interests(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
     Get mutual interests between the current user and another user.
    Compares:
    - political_interest
    - occupation
    - community_role
    - region
    - interests (list[str])
    """

    #  Prevent self-check
    if user_id == current_user.id:
        return []

    # Fetch target user
    result = await db.execute(select(models.User).where(models.User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    mutuals: list[str] = []

    # Compare simple string attributes
    fields_to_compare = [
        "political_interest",
        "occupation",
        "community_role",
        "region",
    ]

    for field in fields_to_compare:
        curr_val = getattr(current_user, field, None)
        target_val = getattr(target_user, field, None)
        if curr_val and target_val and curr_val.lower() == target_val.lower():
            mutuals.append(f"{field.replace('_', ' ').title()}: {curr_val}")

    # Compare list-type interests if both users have them
    if hasattr(current_user, "interests") and hasattr(target_user, "interests"):
        if current_user.interests and target_user.interests:
            shared = set(current_user.interests) & set(target_user.interests)
            mutuals.extend([f"Interest: {i}" for i in shared])

    return mutuals
