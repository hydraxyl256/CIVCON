from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from app.database import get_db
from app.models import User
from app.crud import get_user_by_email, create_user 
from app.config import settings
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import os
import uuid
import logging
from datetime import datetime
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm

router = APIRouter(prefix="/users", tags=["users"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

class UserUpdate(BaseModel):
    first_name: str | None
    last_name: str | None
    email: EmailStr | None
    region: str | None
    district_id: str | None
    county_id: str | None
    sub_county_id: str | None
    parish_id: str | None
    village_id: str | None
    occupation: str | None
    bio: str | None
    political_interest: str | None
    community_role: str | None
    interests: list[str] | None
    notifications: dict | None
    privacy_level: str | None

class UserResponse(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: str
    role: str
    region: str | None
    district_id: str | None
    county_id: str | None
    sub_county_id: str | None
    parish_id: str | None
    village_id: str | None
    occupation: str | None
    bio: str | None
    profile_image: str | None
    political_interest: str | None
    community_role: str | None
    interests: list | None
    notifications: dict | None
    privacy_level: str | None
    created_at: datetime

    class Config:
        from_attribute = True

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = get_user_by_email(db, email=email)
    if user is None:
        raise credentials_exception
    return user

@router.get("/me", response_model=UserResponse)
async def get_profile(current_user: User = Depends(get_current_user)):
    return current_user

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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
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
    if profile_image:
        os.makedirs("static/uploads", exist_ok=True)
        file_extension = profile_image.filename.split('.')[-1]
        filename = f"{uuid.uuid4()}.{file_extension}"
        profile_image_path = f"static/uploads/{filename}"
        with open(profile_image_path, "wb") as f:
            f.write(profile_image.file.read())
        current_user.profile_image = profile_image_path
    db.commit()
    db.refresh(current_user)
    logger.info(f"Profile updated for user {current_user.email}")
    return current_user