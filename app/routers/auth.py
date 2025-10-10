from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta, datetime
from jose import JWTError, jwt
import uuid
import os
import json
import requests
from functools import lru_cache
from pydantic import EmailStr, BaseModel
from typing import AsyncGenerator, Optional, List, Dict
from app.schemas import UserCreate, Token, User
from app.crud import create_user, get_user_by_email, verify_password
from app.database import get_db
from fastapi_sso.sso.google import GoogleSSO
from fastapi_sso.sso.linkedin import LinkedInSSO
from dotenv import load_dotenv
import logging
from app.config import settings

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()  

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes

router = APIRouter(prefix="/auth")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# Dependencies for SSO providers
async def get_google_sso() -> AsyncGenerator[GoogleSSO, None]:
    async with GoogleSSO(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        redirect_uri="http://127.0.0.1:8000/auth/google/callback"
    ) as sso:
        yield sso

async def get_linkedin_sso() -> AsyncGenerator[LinkedInSSO, None]:
    async with LinkedInSSO(
        client_id=os.getenv("LINKEDIN_CLIENT_ID"),
        client_secret=os.getenv("LINKEDIN_CLIENT_SECRET"),
        redirect_uri="http://127.0.0.1:8000/auth/linkedin/callback",
        scope=["r_emailaddress", "r_basicprofile"]
    ) as sso:
        yield sso

# Uganda Locale Service
class Location(BaseModel):
    id: str
    name: str

class UgandaLocaleComplete:
    def __init__(self):
        self.base_url = "https://raw.githubusercontent.com/paulgrammer/ug-locale/main"
        self.districts_data = None
        self.counties_data = None
        self.subcounties_data = None
        self.parishes_data = None
        self.villages_data = None
        self._load_data()

    def _load_data(self):
        try:
            logger.info("Loading Uganda administrative data...")
            self.districts_data = requests.get(f"{self.base_url}/districts.json").json()
            self.counties_data = requests.get(f"{self.base_url}/counties.json").json()
            self.subcounties_data = requests.get(f"{self.base_url}/subcounties.json").json()
            self.parishes_data = requests.get(f"{self.base_url}/parishes.json").json()
            self.villages_data = requests.get(f"{self.base_url}/villages.json").json()
            logger.info("All data loaded successfully!")
        except Exception as e:
            logger.error(f"Error loading data: {e}")
            self.districts_data = []
            self.counties_data = []
            self.subcounties_data = []
            self.parishes_data = []
            self.villages_data = []

    @lru_cache(maxsize=None)
    def get_districts(self) -> List[dict]:
        return [{"id": d["id"], "name": d["name"]} for d in self.districts_data]

    @lru_cache(maxsize=None)
    def get_counties(self, district_id: str) -> List[dict]:
        return [{"id": c["id"], "name": c["name"]} for c in self.counties_data if c.get("district") == district_id]

    @lru_cache(maxsize=None)
    def get_sub_counties(self, county_id: str) -> List[dict]:
        return [{"id": sc["id"], "name": sc["name"]} for sc in self.subcounties_data if sc.get("county") == county_id]

    @lru_cache(maxsize=None)
    def get_parishes(self, sub_county_id: str) -> List[dict]:
        return [{"id": p["id"], "name": p["name"]} for p in self.parishes_data if p.get("subcounty") == sub_county_id]

    @lru_cache(maxsize=None)
    def get_villages(self, parish_id: str) -> List[dict]:
        return [{"id": v["id"], "name": v["name"]} for v in self.villages_data if v.get("parish") == parish_id]

    def find_district_by_id(self, district_id: str) -> Optional[dict]:
        return next((d for d in self.districts_data if d.get("id") == district_id), None)

    def find_county_by_id(self, county_id: str) -> Optional[dict]:
        return next((c for c in self.counties_data if c.get("id") == county_id), None)

    def find_subcounty_by_id(self, subcounty_id: str) -> Optional[dict]:
        return next((sc for sc in self.subcounties_data if sc.get("id") == subcounty_id), None)

    def find_parish_by_id(self, parish_id: str) -> Optional[dict]:
        return next((p for p in self.parishes_data if p.get("id") == parish_id), None)

uga_locale = UgandaLocaleComplete()

# Authentication helpers
async def authenticate_user(db: AsyncSession, email: str, password: str):
    user = await get_user_by_email(db, email)  
    if not user:
        return False
    if not verify_password(password, user.hashed_password):
        return False
    return user


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


#signup Endpoint
@router.post("/signup", response_model=User, status_code=status.HTTP_201_CREATED)
async def signup(
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: EmailStr = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    region: Optional[str] = Form(None),
    district_id: Optional[str] = Form(None),
    county_id: Optional[str] = Form(None),
    sub_county_id: Optional[str] = Form(None),
    parish_id: Optional[str] = Form(None),
    village_id: Optional[str] = Form(None),
    occupation: Optional[str] = Form(None),
    bio: Optional[str] = Form(None),
    political_interest: Optional[str] = Form(None),
    community_role: Optional[str] = Form(None),
    interests: Optional[str] = Form(None),
    privacy_level: Optional[str] = Form(None),
    profile_image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db)
):
    # Password check
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    # Parse JSON fields
    try:
        interests_list = json.loads(interests) if interests else []
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format for interests")

    # Check email uniqueness
    existing_user = await get_user_by_email(db, email)
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Handle profile image upload
    profile_image_path = None
    if profile_image:
        os.makedirs("static/uploads", exist_ok=True)
        file_extension = profile_image.filename.split('.')[-1] if profile_image.filename else "bin"
        filename = f"{uuid.uuid4()}.{file_extension}"
        profile_image_path = os.path.join("static", "uploads", filename)
        with open(profile_image_path, "wb") as f:
            f.write(profile_image.file.read())
        profile_image.file.close()

    # Build UserCreate model
    user_payload = UserCreate(
        first_name=first_name,
        last_name=last_name,
        email=email,
        password=password,
        confirm_password=confirm_password,
        region=region,
        district_id=district_id,
        county_id=county_id,
        sub_county_id=sub_county_id,
        parish_id=parish_id,
        village_id=village_id,
        occupation=occupation,
        bio=bio,
        political_interest=political_interest,
        community_role=community_role,
        interests=interests_list,
        privacy_level=privacy_level or "public"
    )

    # Create user
    created_user = await create_user(db, user_payload, profile_image_path)

    return created_user


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, form_data.username, form_data.password)  # ✅ await
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


# Location Endpoints (unchanged)
@router.get("/locations/districts", response_model=List[Location], summary="Get all districts")
async def get_districts():
    districts = uga_locale.get_districts()
    return districts

@router.get("/locations/counties/{district_id}", response_model=List[Location], summary="Get counties in a district")
async def get_counties(district_id: str):
    district = uga_locale.find_district_by_id(district_id)
    if not district:
        raise HTTPException(status_code=404, detail=f"District with id '{district_id}' not found")
    counties = uga_locale.get_counties(district_id)
    return counties

@router.get("/locations/sub-counties/{county_id}", response_model=List[Location], summary="Get sub-counties in a county")
async def get_sub_counties(county_id: str):
    county = uga_locale.find_county_by_id(county_id)
    if not county:
        raise HTTPException(status_code=404, detail=f"County with id '{county_id}' not found")
    sub_counties = uga_locale.get_sub_counties(county_id)
    if not sub_counties:
        raise HTTPException(status_code=404, detail=f"No sub-counties found for county '{county['name']}' (id: {county_id})")
    return sub_counties

@router.get("/locations/parishes/{sub_county_id}", response_model=List[Location], summary="Get parishes in a sub-county")
async def get_parishes(sub_county_id: str):
    subcounty = uga_locale.find_subcounty_by_id(sub_county_id)
    if not subcounty:
        raise HTTPException(status_code=404, detail=f"Sub-county with id '{sub_county_id}' not found")
    parishes = uga_locale.get_parishes(sub_county_id)
    if not parishes:
        raise HTTPException(status_code=404, detail=f"No parishes found for sub-county '{subcounty['name']}' (id: {sub_county_id})")
    return parishes

@router.get("/locations/villages/{parish_id}", response_model=List[Location], summary="Get villages in a parish")
async def get_villages(parish_id: str):
    parish = uga_locale.find_parish_by_id(parish_id)
    if not parish:
        raise HTTPException(status_code=404, detail=f"Parish with id '{parish_id}' not found")
    villages = uga_locale.get_villages(parish_id)
    if not villages:
        raise HTTPException(status_code=404, detail=f"No villages found for parish '{parish['name']}' (id: {parish_id})")
    return villages

# Social Login Endpoints
@router.get("/google/login")
async def google_login(google_sso: GoogleSSO = Depends(get_google_sso)):
    """Initiate Google login redirect."""
    try:
        redirect_response = await google_sso.get_login_redirect()
        logger.info("Google login redirect initiated")
        return redirect_response
    except Exception as e:
        logger.exception("Google login initiation failed")
        raise HTTPException(status_code=400, detail=f"Google login initiation failed: {str(e)}")

@router.get("/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db), google_sso: GoogleSSO = Depends(get_google_sso)):
    """Handle Google callback, create/link user, return JWT."""
    try:
        logger.info(f"Google callback received: {request.query_params}")
        user_info = await google_sso.verify_and_process(request)
        logger.info(f"Google user info: {user_info}")
    except Exception as e:
        logger.exception("Google SSO failed")
        raise HTTPException(status_code=400, detail=f"Google SSO failed: {str(e)}")

    # Find or create user
    db_user = get_user_by_email(db, user_info.email)
    if not db_user:
        user_create = UserCreate(
            first_name=getattr(user_info, "given_name", None) or "Unknown",
            last_name=getattr(user_info, "family_name", None) or "User",
            email=user_info.email,
            password="",
            confirm_password="",
            privacy_level="public",
            interests=[],
            notifications={"email": True, "sms": False, "push": True}
        )
        try:
            db_user = create_user(db, user_create, None, is_social=True)
        except Exception as e:
            logger.exception("Failed to create user from Google SSO")
            raise HTTPException(status_code=500, detail="Failed to create user from Google SSO")

    # Link Google ID if not set
    try:
        if not getattr(db_user, "google_id", None):
            db_user.google_id = getattr(user_info, "subject", None) or getattr(user_info, "sub", None)
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
    except Exception:
        logger.exception("Failed to link google_id")

    # Generate JWT
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": db_user.email}, expires_delta=access_token_expires)

    # Redirect to frontend with token
    frontend_redirect = f"http://localhost:5173/auth/callback?access_token={access_token}"
    return RedirectResponse(url=frontend_redirect)

@router.get("/linkedin/login")
async def linkedin_login(linkedin_sso: LinkedInSSO = Depends(get_linkedin_sso)):
    """Initiate LinkedIn login redirect."""
    try:
        redirect_response = await linkedin_sso.get_login_redirect()
        logger.info("LinkedIn login redirect initiated")
        return redirect_response
    except Exception as e:
        logger.exception("LinkedIn login initiation failed")
        raise HTTPException(status_code=400, detail=f"LinkedIn login initiation failed: {str(e)}")

@router.get("/linkedin/callback")
async def linkedin_callback(request: Request, db: Session = Depends(get_db), linkedin_sso: LinkedInSSO = Depends(get_linkedin_sso)):
    """Handle LinkedIn callback, create/link user, return JWT."""
    try:
        logger.info(f"LinkedIn callback received: {request.query_params}")
        user_info = await linkedin_sso.verify_and_process(request)
        logger.info(f"LinkedIn user info: {user_info}")
    except Exception as e:
        logger.exception("LinkedIn SSO failed")
        raise HTTPException(status_code=400, detail=f"LinkedIn SSO failed: {str(e)}")

    first_name = getattr(user_info, "localized_first_name", None) or "Unknown"
    last_name = getattr(user_info, "localized_last_name", None) or "User"
    email = getattr(user_info, "email_address", None)

    if not email:
        raise HTTPException(status_code=400, detail="LinkedIn did not return an email address")

    # Find or create user
    db_user = get_user_by_email(db, email)
    if not db_user:
        user_create = UserCreate(
            first_name=first_name,
            last_name=last_name,
            email=email,
            password="",
            confirm_password="",
            privacy_level="public",
            interests=[],
            notifications={"email": True, "sms": False, "push": True}
        )
        try:
            db_user = create_user(db, user_create, None, is_social=True)
        except Exception as e:
            logger.exception("Failed to create user from LinkedIn SSO")
            raise HTTPException(status_code=500, detail="Failed to create user from LinkedIn SSO")

    # Link LinkedIn ID if not set
    try:
        if not getattr(db_user, "linkedin_id", None):
            db_user.linkedin_id = getattr(user_info, "id", None)
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
    except Exception:
        logger.exception("Failed to link linkedin_id")

    # Generate JWT
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": db_user.email}, expires_delta=access_token_expires)

    # Redirect to frontend with token
    frontend_redirect = f"http://localhost:5173/auth/callback?access_token={access_token}"
    return RedirectResponse(url=frontend_redirect)
