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
from routers.auth import upload_to_cloudinary
from app.schemas import  UserResponse, UserUpdate
import cloudinary.uploader
from app.schemas import UserOut
from app.models import Post, Comment, Vote
from sqlalchemy import delete



logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm

router = APIRouter(prefix="/users", tags=["users"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")




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


@router.put("/me", response_model=UserResponse)
async def update_profile(
    first_name: str = Form(None),
    last_name: str = Form(None),
    email: EmailStr = Form(None),
    region: str = Form(None),
    district_id: str = Form(None),
    county_id: str = Form(None),
    sub_county_id: str = Form(None),
    parish_id: str = Form(None),
    village_id: str = Form(None),
    occupation: str = Form(None),
    bio: str = Form(None),
    political_interest: str = Form(None),
    community_role: str = Form(None),
    interests: str = Form(None),
    notifications: str = Form(None),
    privacy_level: str = Form(None),
    profile_image: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    #  Update user fields
    update_data = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "region": region,
        "district_id": district_id,
        "county_id": county_id,
        "sub_county_id": sub_county_id,
        "parish_id": parish_id,
        "village_id": village_id,
        "occupation": occupation,
        "bio": bio,
        "political_interest": political_interest,
        "community_role": community_role,
        "interests": json.loads(interests) if interests else None,
        "notifications": json.loads(notifications) if notifications else None,
        "privacy_level": privacy_level
    }

    for key, value in update_data.items():
        if value is not None:
            setattr(current_user, key, value)

    # Handle profile image
    if profile_image:
        os.makedirs("static/uploads", exist_ok=True)
        file_extension = profile_image.filename.split('.')[-1]
        filename = f"{uuid.uuid4()}.{file_extension}"
        profile_image_path = f"static/uploads/{filename}"
        with open(profile_image_path, "wb") as f:
            f.write(profile_image.file.read())
        current_user.profile_image = profile_image_path

    # Update or create MP record if user is MP
    if current_user.role == Role.MP:
        result = await db.execute(select(MP).where(MP.user_id == current_user.id))
        existing_mp = result.scalars().first()

        if existing_mp:
            existing_mp.name = f"{current_user.first_name} {current_user.last_name}"
            existing_mp.phone_number = current_user.phone_number
            existing_mp.email = current_user.email
            existing_mp.district_id = current_user.district_id
            existing_mp.updated_at = datetime.utcnow()
            db.add(existing_mp)
        else:
            new_mp = MP(
                name=f"{current_user.first_name} {current_user.last_name}",
                phone_number=current_user.phone_number,
                email=current_user.email,
                district_id=current_user.district_id,
                user_id=current_user.id
            )
            db.add(new_mp)

    # Commit changes
    await db.commit()
    await db.refresh(current_user)
    logger.info(f"Profile updated for user {current_user.email}")

    #  Return updated user
    return current_user


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

        logger.info(f"💀 User {current_user.email} deleted successfully.")
        return {"message": "Your account and all data have been permanently deleted."}

    except Exception as e:
        logger.exception("Error deleting account")
        raise HTTPException(status_code=500, detail="Internal server error")

