from fastapi import HTTPException, status
import random
import string
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.models import User, Role, UssdSession
from app.schemas import UserCreate
from passlib.context import CryptContext
from passlib.hash import bcrypt

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


#  PASSWORD HELPERS 

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


#  ROLE DERIVATION 

def derive_role(community_role: Optional[str]) -> Role:
    if community_role:
        cr_lower = community_role.lower()
        if "official" in cr_lower:
            return Role.MP
        elif "journalist" in cr_lower:
            return Role.JOURNALIST
    return Role.CITIZEN


#  Helper: Generate a unique username
async def generate_unique_username(first_name: str, last_name: str, db: AsyncSession) -> str:
    """
    Generate a unique username from first+last name.
    Adds random digits if a duplicate exists.
    Example: eronlaban, eronlaban_482, etc.
    """
    base_username = f"{first_name.lower()}{last_name.lower()}".replace(" ", "")
    username = base_username

    # Check for duplicates and retry with random suffix
    while True:
        result = await db.execute(select(User).where(User.username == username))
        existing_user = result.scalar_one_or_none()
        if not existing_user:
            break  # unique username found
        suffix = ''.join(random.choices(string.digits, k=3))
        username = f"{base_username}_{suffix}"

    return username


#  CREATE USER 
async def create_user(db: AsyncSession, user: "UserCreate", profile_image_path: str = None):
    hashed_password = bcrypt.hash(user.password)

    # Validate interests field
    interests = user.interests if isinstance(user.interests, list) else []

    #  Auto-generate unique username
    username = await generate_unique_username(user.first_name, user.last_name, db)

    #  Create and save user
    db_user = User(
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        username=username,  
        hashed_password=hashed_password,
        role=user.role,
        region=user.region,
        district_id=user.district_id,
        county_id=user.county_id,
        occupation=user.occupation,
        bio=user.bio,
        profile_image=profile_image_path,
        political_interest=user.political_interest,
        community_role=user.community_role,
        interests=interests,
        privacy_level=user.privacy_level,
    )

    db.add(db_user)
    try:
        await db.commit()
        await db.refresh(db_user)
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating user: {str(e)}",
        )

    return db_user

# GET USERS 

async def get_user_by_email(db: AsyncSession, email: str):
    result = await db.execute(select(User).where(User.email == email))
    return result.scalars().first()


async def get_user_by_google_id(db: AsyncSession, google_id: str) -> Optional[User]:
    result = await db.execute(select(User).filter_by(google_id=google_id))
    return result.scalars().first()


async def get_user_by_linkedin_id(db: AsyncSession, linkedin_id: str) -> Optional[User]:
    result = await db.execute(select(User).filter_by(linkedin_id=linkedin_id))
    return result.scalars().first()




async def get_mps_by_district(db: AsyncSession, district_id: str) -> list[User]:
    return db.query(User).filter(User.role == Role.MP, User.district_id == district_id).all()

async def get_ussd_session(db: AsyncSession, phone_number: str, session_id: str) -> Optional[dict]:
    # Implement session query from ussd_sessions table
    session = db.query(UssdSession).filter(UssdSession.phone_number == phone_number, UssdSession.session_id == session_id).first()
    if session:
        return {"step": session.current_step, "data": session.user_data}
    return None

async def save_ussd_session(db: AsyncSession, phone_number: str, session_id: str, step: str, data: dict):
    session = db.query(UssdSession).filter(UssdSession.phone_number == phone_number, UssdSession.session_id == session_id).first()
    if session:
        session.current_step = step
        session.user_data = data
    else:
        session = UssdSession(
            phone_number=phone_number,
            session_id=session_id,
            current_step=step,
            user_data=data
        )
        db.add(session)
    db.commit()