from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Body, Request
from fastapi.responses import RedirectResponse
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List
from functools import lru_cache
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from pydantic import EmailStr
from jose import jwt, JWTError
from passlib.context import CryptContext
import cloudinary
import cloudinary.uploader
import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession
from app.utils.email_utils import send_reset_email
from app.database import get_db
from app.schemas import UserCreate, User, Token, UserOut
from app.crud import create_user, get_user_by_email, verify_password  
from app.config import settings  
from app.schemas import ResetPasswordSchema
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.future import select
import logging
import requests
from pydantic import BaseModel 



router = APIRouter(prefix="/auth",
                    tags=["auth"])

oauth = OAuth()

# Setup: secrets & services
SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Redis for token blacklist (logout). Use env var for URL in production (e.g., Upstash or Render Redis)
REDIS_URL = settings.redis_url
redis = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

# Cloudinary config from env (set these in Render/Railway)
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
    secure=True,
)

# Register Google
oauth.register(
    name="google",
    client_id=settings.google_client_id,
    client_secret=settings.google_client_secret,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# Register LinkedIn
oauth.register(
    name="linkedin",
    client_id=settings.linkedin_client_id,
    client_secret=settings.linkedin_client_secret,
    access_token_url="https://www.linkedin.com/oauth/v2/accessToken",
    access_token_params=None,
    authorize_url="https://www.linkedin.com/oauth/v2/authorization",
    api_base_url="https://api.linkedin.com/v2",
    client_kwargs={"scope": "r_liteprofile r_emailaddress"},
)

# Configure logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
            # Set to empty lists on failure
            self.districts_data = []
            self.counties_data = []
            self.subcounties_data = []
            self.parishes_data = []
            self.villages_data = []

    @lru_cache(maxsize=None)
    def get_districts(self) -> List[Location]:
        return [Location(id=d["id"], name=d["name"]) for d in self.districts_data]

    @lru_cache(maxsize=None)
    def get_counties(self, district_id: str) -> List[Location]:
        return [Location(id=c["id"], name=c["name"]) for c in self.counties_data if c.get("district") == district_id]

    @lru_cache(maxsize=None)
    def get_sub_counties(self, county_id: str) -> List[Location]:
        return [Location(id=sc["id"], name=sc["name"]) for sc in self.subcounties_data if sc.get("county") == county_id]

    @lru_cache(maxsize=None)
    def get_parishes(self, sub_county_id: str) -> List[Location]:
        return [Location(id=p["id"], name=p["name"]) for p in self.parishes_data if p.get("subcounty") == sub_county_id]

    @lru_cache(maxsize=None)
    def get_villages(self, parish_id: str) -> List[Location]:
        return [Location(id=v["id"], name=v["name"]) for v in self.villages_data if v.get("parish") == parish_id]

    def find_district_by_id(self, district_id: str) -> Optional[dict]:
        return next((d for d in self.districts_data if d.get("id") == district_id), None)

    def find_county_by_id(self, county_id: str) -> Optional[dict]:
        return next((c for c in self.counties_data if c.get("id") == county_id), None)

    def find_subcounty_by_id(self, subcounty_id: str) -> Optional[dict]:
        return next((sc for sc in self.subcounties_data if sc.get("id") == subcounty_id), None)

    def find_parish_by_id(self, parish_id: str) -> Optional[dict]:
        return next((p for p in self.parishes_data if p.get("id") == parish_id), None)

# Instantiate
uga_locale = UgandaLocaleComplete()


# Helpers
def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Upload file to Cloudinary in a thread to avoid blocking event loop
async def upload_to_cloudinary(file: UploadFile, folder: str = "civcon/profiles") -> str:
    content = await file.read()  # read bytes (safe for reasonably sized profile images)
    # run the synchronous cloudinary.uploader.upload in a thread
    def _upload():
        return cloudinary.uploader.upload(
            content,
            folder=folder,
            resource_type="auto",
            overwrite=True,
        )
    result = await asyncio.to_thread(_upload)
    # Cloudinary returns 'secure_url' often
    return result.get("secure_url") or result.get("url")


# Authentication helpers
async def authenticate_user(db: AsyncSession, email: str, password: str):
    user = await get_user_by_email(db, email)
    if not user:
        return None
    # verify_password may be sync or async depending on your implementation; passlib verify is sync but quick
    if not verify_password(password, user.hashed_password):
        return None
    return user

# Dependency to get current user from token and check blacklist
async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> UserOut:
    # Check blacklist first
    if await redis.get(f"blacklist:{token}"):
        raise HTTPException(status_code=401, detail="Token revoked")

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

    user = await get_user_by_email(db, email)
    if not user:
        raise credentials_exception

    # Convert DB model to schema UserOut (pydantic from_attributes must be enabled)
    return UserOut.model_validate(user)


# Endpoints
@router.post("/signup", response_model=User, status_code=status.HTTP_201_CREATED)
async def signup(
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: EmailStr = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    profile_image: Optional[UploadFile] = File(None),
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
    interests: Optional[str] = Form(None),  # JSON string expected from client
    privacy_level: Optional[str] = Form("public"),
    db: AsyncSession = Depends(get_db)
):
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    existing = await get_user_by_email(db, email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    # handle interests string -> list
    try:
        parsed_interests = [] if not interests else __import__("json").loads(interests)
        if not isinstance(parsed_interests, list):
            parsed_interests = []
    except Exception:
        parsed_interests = []

    profile_image_url = None
    if profile_image:
        # optional file validation (size/type) can be added
        try:
            profile_image_url = await upload_to_cloudinary(profile_image, folder="civcon/profiles")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Image upload failed: {str(e)}")

    user_create = UserCreate(
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
        interests=parsed_interests,
        privacy_level=privacy_level,
    )

    # create_user is expected async and to accept profile_image_path (we pass url)
    created = await create_user(db, user_create, profile_image_path=profile_image_url)
    # created should be ORM model; pydantic schema User must have from_attributes True
    return User.model_validate(created)


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token({"sub": user.email}, expires_delta=access_token_expires)
    return {"access_token": token, "token_type": "bearer"}


# Forgot password (sends token) - actual sending via FastMail or an external service
@router.post("/forgot-password")
async def forgot_password(email: EmailStr, db: AsyncSession = Depends(get_db)):
    user = await get_user_by_email(db, email)
    if not user:
        # Don't reveal existence
        return {"message": "If that email exists, a reset link was sent."}

    reset_token = create_access_token({"sub": user.email, "scope": "password_reset"}, expires_delta=timedelta(minutes=30))
    reset_link = f"{os.getenv('FRONTEND_URL', 'https://civ-con-sh2j.vercel.app/')}/reset-password?token={reset_token}"

    # Send the actual email
    await send_reset_email(user.email, reset_link)

    return {"message": "Password reset email sent successfully."}


@router.post("/reset-password")
async def reset_password(
    data: ResetPasswordSchema = Body(...),
    db: AsyncSession = Depends(get_db)
):
    token = data.token
    new_password = data.new_password

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid token")

    if payload.get("scope") != "password_reset":
        raise HTTPException(status_code=400, detail="Invalid token scope")

    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=400, detail="Invalid token payload")

    user = await get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.hashed_password = get_password_hash(new_password)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Optionally blacklist all old tokens for the user for some TTL
    await redis.setex(f"blacklist_user:{email}", 60 * 60, "true")

    return {"message": "Password reset successful"}


# Logout => blacklist token until it expires
@router.post("/logout")
async def logout(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid token")

    exp = payload.get("exp")
    if not exp:
        raise HTTPException(status_code=400, detail="Invalid token payload")
    ttl = int(exp - datetime.utcnow().timestamp())
    if ttl > 0:
        await redis.setex(f"blacklist:{token}", ttl, "true")
    return {"message": "Logged out"}


#  protected endpoint
@router.get("/me", response_model=UserOut)
async def me(user: UserOut = Depends(get_current_user)):
    return user



#  GOOGLE 
@router.get("/google/login")
async def google_login(request: Request):
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI") or f"{settings.backend_url}/auth/google/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/google/callback")
async def google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo")

    if not user_info:
        raise HTTPException(status_code=400, detail="Google login failed")

    email = user_info.get("email")
    name = user_info.get("name", "")
    picture = user_info.get("picture", "")

    if not email:
        raise HTTPException(status_code=400, detail="Email not found in Google response")

    #  Async query
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    #  Create user if new
    if not user:
        user = User(
            email=email,
            fullname=name,
            profile_image=picture,
            is_active=True,
            provider="google"
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    #  Generate JWT
    access_token = create_access_token({"sub": str(user.id)})

    #  Auto redirect to frontend homepage with token
    redirect_url = f"{settings.frontend_url}/?token={access_token}"
    return RedirectResponse(url=redirect_url)



#  LINKEDIN 
@router.get("/linkedin/login")
async def linkedin_login(request: Request):
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI") or f"{settings.backend_url}/auth/linkedin/callback"
    return await oauth.linkedin.authorize_redirect(request, redirect_uri)


@router.get("/linkedin/callback")
async def linkedin_callback(request: Request, db: AsyncSession = Depends(get_db)):
    token = await oauth.linkedin.authorize_access_token(request)
    access_token = token.get("access_token")

    if not access_token:
        raise HTTPException(status_code=400, detail="LinkedIn authorization failed")

    # Fetch user info
    profile = await oauth.linkedin.get(
        "me?projection=(id,localizedFirstName,localizedLastName,profilePicture(displayImage~:playableStreams))",
        token=token
    )
    email_resp = await oauth.linkedin.get(
        "emailAddress?q=members&projection=(elements*(handle~))",
        token=token
    )

    data = profile.json()
    email_data = email_resp.json()
    email = email_data["elements"][0]["handle~"]["emailAddress"]
    name = f"{data.get('localizedFirstName', '')} {data.get('localizedLastName', '')}"
    picture = (
        data.get("profilePicture", {})
            .get("displayImage~", {})
            .get("elements", [{}])[-1]
            .get("identifiers", [{}])[0]
            .get("identifier")
    )

    #  Async query
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            email=email,
            fullname=name,
            profile_image=picture,
            is_active=True,
            provider="linkedin"
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    #  Generate JWT
    jwt_token = create_access_token({"sub": str(user.id)})

    #  Auto redirect to frontend homepage with token
    redirect_url = f"{settings.frontend_url}/?token={jwt_token}"
    return RedirectResponse(url=redirect_url)
