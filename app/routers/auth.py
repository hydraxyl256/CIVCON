import asyncio
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from functools import cache

import cloudinary
import cloudinary.uploader
import requests
from authlib.integrations.starlette_client import OAuth
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi_limiter.depends import RateLimiter
from jose import JWTError
from pydantic import EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.config import settings
from app.core.auth import (
    ACCESS_TOKEN_TYPE,
    PASSWORD_RESET_SCOPE,
    REFRESH_TOKEN_TYPE,
    PasswordValidationError,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    validate_password_strength,
    verify_password,
)
from app.core.cookies import (
    clear_auth_cookies,
    read_refresh_cookie,
    set_auth_cookies,
    set_csrf_cookie,
)
from app.crud import create_user, get_user_by_email
from app.database import get_db
from app.dependencies.auth import access_token_from_cookie_or_header
from app.models import Session as AuthSession
from app.models import User
from app.observability.auth_log import log_event
from app.redis_client import get_redis
from app.schemas import (
    AuthSessionResponse,
    ForgotPasswordRequest,
    Location,
    OAuthBootstrapExchangeRequest,
    RefreshTokenRequest,
    ResetPasswordSchema,
    Token,
    UserCreate,
    UserOut,
)
from app.utils.email_utils import send_reset_email


def _ip(request: Request | None) -> str | None:
    """Best-effort client IP. Returns None when no request is bound."""
    if request is None or request.client is None:
        return None
    return request.client.host




router = APIRouter(prefix="/auth",
                    tags=["auth"])

oauth = OAuth()

# Setup: secrets & services
SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# Redis client — single source of truth lives in `app/redis_client.py`.
# Callers await `get_redis()` per request so we don't allocate a second
# connection pool to the same REDIS_URL at import time.

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
#
# LinkedIn deprecated the legacy `r_liteprofile` and `r_emailaddress`
# scopes in 2023; new applications are required to use OpenID Connect.
# We follow the same metadata-driven registration pattern as Google,
# which makes `authorize_access_token` populate `userinfo` automatically
# and removes the need for separate /me + /emailAddress fetches in the
# callback.
oauth.register(
    name="linkedin",
    client_id=settings.linkedin_client_id,
    client_secret=settings.linkedin_client_secret,
    server_metadata_url=(
        "https://www.linkedin.com/oauth/v2/.well-known/openid-configuration"
    ),
    client_kwargs={"scope": "openid profile email"},
)

# Configure logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
            # 5-second timeout per request: the data is small JSON files
            # served from GitHub's raw CDN. A longer wait would only
            # delay startup on a slow or unreachable connection; the
            # endpoint falls back to empty lists on any failure.
            self.districts_data = requests.get(f"{self.base_url}/districts.json", timeout=5).json()
            self.counties_data = requests.get(f"{self.base_url}/counties.json", timeout=5).json()
            self.subcounties_data = requests.get(f"{self.base_url}/subcounties.json", timeout=5).json()
            self.parishes_data = requests.get(f"{self.base_url}/parishes.json", timeout=5).json()
            self.villages_data = requests.get(f"{self.base_url}/villages.json", timeout=5).json()
            logger.info("All data loaded successfully!")
        except Exception as e:
            logger.error(f"Error loading data: {e}")
            # Set to empty lists on failure
            self.districts_data = []
            self.counties_data = []
            self.subcounties_data = []
            self.parishes_data = []
            self.villages_data = []

    @cache
    def get_districts(self) -> list[Location]:
        return [Location(id=d["id"], name=d["name"]) for d in self.districts_data]

    @cache
    def get_counties(self, district_id: str) -> list[Location]:
        return [Location(id=c["id"], name=c["name"]) for c in self.counties_data if c.get("district") == district_id]

    @cache
    def get_sub_counties(self, county_id: str) -> list[Location]:
        return [Location(id=sc["id"], name=sc["name"]) for sc in self.subcounties_data if sc.get("county") == county_id]

    @cache
    def get_parishes(self, sub_county_id: str) -> list[Location]:
        return [Location(id=p["id"], name=p["name"]) for p in self.parishes_data if p.get("subcounty") == sub_county_id]

    @cache
    def get_villages(self, parish_id: str) -> list[Location]:
        return [Location(id=v["id"], name=v["name"]) for v in self.villages_data if v.get("parish") == parish_id]

    def find_district_by_id(self, district_id: str) -> dict | None:
        return next((d for d in self.districts_data if d.get("id") == district_id), None)

    def find_county_by_id(self, county_id: str) -> dict | None:
        return next((c for c in self.counties_data if c.get("id") == county_id), None)

    def find_subcounty_by_id(self, subcounty_id: str) -> dict | None:
        return next((sc for sc in self.subcounties_data if sc.get("id") == subcounty_id), None)

    def find_parish_by_id(self, parish_id: str) -> dict | None:
        return next((p for p in self.parishes_data if p.get("id") == parish_id), None)

# Instantiate
uga_locale = UgandaLocaleComplete()


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


# ============================================================================
# Authentication helpers
# ============================================================================
async def authenticate_user(db: AsyncSession, email: str, password: str):
    """Verify a user's email + password. Returns the User on success, None on failure."""
    user = await get_user_by_email(db, email)
    if not user:
        return None
    if not user.hashed_password:
        return None  # OAuth-only account with no password set
    if not verify_password(password, user.hashed_password):
        return None
    return user


def _credentials_error(detail: str = "Could not validate credentials") -> HTTPException:
    """Return a 401 with the standard `WWW-Authenticate` error fields so the SPA can react."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={
            "WWW-Authenticate": 'Bearer error="invalid_token", error_description="' + detail + '"'
        },
    )


async def _create_session_record(
    db: AsyncSession,
    user: User,
    family_id: str,
    jti: str,
    expires_at: datetime,
    request: Request | None = None,
) -> AuthSession:
    """Persist a Session row so it can be listed and revoked."""
    session = AuthSession(
        user_id=user.id,
        family_id=family_id,
        current_jti=jti,
        user_agent=(request.headers.get("user-agent") if request else None),
        ip_address=(request.client.host if request and request.client else None),
        expires_at=expires_at,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _issue_token_pair(
    db: AsyncSession,
    user: User,
    remember_me: bool = False,
    request: Request | None = None,
) -> Token:
    """
    Issue an access + refresh token pair and persist a Session row.

    Returns a Token response containing both tokens, expires_in (seconds for
    the access token), and the public user object.
    """
    access_token, access_exp = create_access_token(
        {"sub": user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        token_type=ACCESS_TOKEN_TYPE,
    )
    refresh_token, refresh_exp, family_id = create_refresh_token(
        user.email, family=None, remember_me=remember_me
    )

    # Decode the just-issued refresh token to capture its jti
    refresh_payload = decode_token(refresh_token)
    jti = refresh_payload["jti"]

    # One round-trip: session row + last_login_at in a single commit.
    # Previously this was two separate commits which doubled the
    # write latency on every login (and OAuth callback).
    session = AuthSession(
        user_id=user.id,
        family_id=family_id,
        current_jti=jti,
        user_agent=(request.headers.get("user-agent") if request else None),
        ip_address=(request.client.host if request and request.client else None),
        expires_at=refresh_exp,
    )
    db.add(session)
    user.last_login_at = datetime.now(tz=UTC)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=int((access_exp - datetime.now(tz=UTC)).total_seconds()),
        user=UserOut.model_validate(user),
    )


def _session_response_with_cookies(
    token_pair: Token,
    *,
    remember_me: bool = False,
) -> JSONResponse:
    """Build a JSONResponse that sets the auth + CSRF cookies.

    This is the F-008 response shape: the body is an
    :class:`AuthSessionResponse` (just ``user`` + ``expires_in``) and
    the access/refresh/CSRF tokens are delivered as HttpOnly cookies on
    the response. The function is shared by ``/auth/login``,
    ``/auth/signup``, ``/auth/refresh``, and the OAuth exchange so the
    cookie-issuing policy stays in one place.
    """
    body = AuthSessionResponse(
        user=token_pair.user,
        expires_in=token_pair.expires_in,
    )
    response = JSONResponse(status_code=200, content=body.model_dump(mode="json"))
    set_auth_cookies(
        response,
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token or "",
        remember_me=remember_me,
    )
    set_csrf_cookie(response)
    return response


# ============================================================================
# OAuth bootstrap-code handoff
# ============================================================================
# The OAuth callback MUST NOT return tokens in the redirect query string:
# they would end up in browser history, server access logs, and the
# `Referer` header on any subsequent navigation. Instead we mint a
# short-lived, single-use bootstrap code, write the token pair into Redis
# keyed by that code, and redirect to the SPA with only the code in the
# query string. The SPA immediately POSTs the code to
# `/auth/oauth/exchange`, which deletes the Redis entry and returns the
# full Token. The code is single-use (DEL on read) and 60-second TTL.
import json as _json
import secrets as _secrets

OAUTH_BOOTSTRAP_TTL_SECONDS = 60


async def _mint_bootstrap_code(token_pair: Token) -> str:
    """
    Store a Token in Redis under a one-time-use code. Returns the code.

    The code is 32 url-safe random bytes (~43 chars). Single-use: the
    `/auth/oauth/exchange` endpoint deletes the key on read. 60-second
    TTL means even if the SPA never calls exchange, the token pair
    self-destructs quickly.
    """
    code = _secrets.token_urlsafe(32)
    payload = _json.dumps(
        {
            "access_token": token_pair.access_token,
            "refresh_token": token_pair.refresh_token,
            "expires_in": token_pair.expires_in,
            "user": token_pair.user.model_dump(mode="json"),
        }
    )
    r = await get_redis()
    await r.setex(f"oauth:bootstrap:{code}", OAUTH_BOOTSTRAP_TTL_SECONDS, payload)
    return code


async def _consume_bootstrap_code(code: str) -> dict | None:
    """
    Atomically read-and-delete a bootstrap code. Returns the stored
    payload (or None if the code is unknown / expired / already used).
    """
    key = f"oauth:bootstrap:{code}"
    # GETDEL is the atomic "read and delete" we want. Available in redis-py
    # 4.x+ and Redis 6.2+.
    r = await get_redis()
    raw = await r.getdel(key)
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except (TypeError, ValueError):
        return None


def _split_full_name(full_name: str) -> tuple[str, str]:
    """Split a 'First Last' string into (first, last) safely.

    Falls back to sensible placeholders when the OAuth provider returns an
    empty string or only a single token, so the User row insert can satisfy
    the NOT NULL constraints on first_name and last_name.
    """
    if not full_name or not full_name.strip():
        return ("User", "")
    parts = full_name.strip().split(maxsplit=1)
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], parts[1])


def _username_from_email(email: str) -> str:
    """Derive a base username from the local-part of an email."""
    local = (email or "").split("@", 1)[0]
    # Strip characters that are not allowed in our username column
    cleaned = "".join(ch for ch in local if ch.isalnum() or ch in {"_", "."})
    return (cleaned or "user")[:30]


async def _ensure_unique_username(db: AsyncSession, email: str) -> str:
    """Pick a username derived from the email that is not yet taken."""
    base = _username_from_email(email)
    candidate = base
    suffix = 1
    while True:
        result = await db.execute(select(User).where(User.username == candidate))
        if result.scalar_one_or_none() is None:
            return candidate
        suffix += 1
        candidate = f"{base}{suffix}"[:30]

# Local alias — kept so endpoints inside this router file can declare
# `Depends(get_current_user)` and receive a `UserOut`. The behaviour is
# the same as `app.dependencies.auth.get_current_user` (blacklist +
# token-type + is_active checks) — we project the model up to a
# Pydantic schema here so the response_model is straightforward.
async def get_current_user(
    token: str = Depends(access_token_from_cookie_or_header),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    from app.dependencies.auth import get_current_user as _canonical

    user = await _canonical(token=token, db=db)
    return UserOut.model_validate(user)


# Uganda location endpoints
# Districts
@router.get("/locations/districts", response_model=list[Location], summary="Get all districts")
async def get_districts():
    districts = uga_locale.get_districts()
    return districts

# Counties in a district
@router.get("/locations/counties/{district_id}", response_model=list[Location], summary="Get counties in a district")
async def get_counties(district_id: str):
    district = uga_locale.find_district_by_id(district_id)
    if not district:
        raise HTTPException(status_code=404, detail=f"District with id '{district_id}' not found")
    counties = uga_locale.get_counties(district_id)
    return counties

# Sub-counties in a county
@router.get("/locations/sub-counties/{county_id}", response_model=list[Location], summary="Get sub-counties in a county")
async def get_sub_counties(county_id: str):
    county = uga_locale.find_county_by_id(county_id)
    if not county:
        raise HTTPException(status_code=404, detail=f"County with id '{county_id}' not found")
    sub_counties = uga_locale.get_sub_counties(county_id)
    if not sub_counties:
        raise HTTPException(status_code=404, detail=f"No sub-counties found for county '{county['name']}' (id: {county_id})")
    return sub_counties

# Parishes in a sub-county
@router.get("/locations/parishes/{sub_county_id}", response_model=list[Location], summary="Get parishes in a sub-county")
async def get_parishes(sub_county_id: str):
    subcounty = uga_locale.find_subcounty_by_id(sub_county_id)
    if not subcounty:
        raise HTTPException(status_code=404, detail=f"Sub-county with id '{sub_county_id}' not found")
    parishes = uga_locale.get_parishes(sub_county_id)
    if not parishes:
        raise HTTPException(status_code=404, detail=f"No parishes found for sub-county '{subcounty['name']}' (id: {sub_county_id})")
    return parishes

# Villages in a parish
@router.get("/locations/villages/{parish_id}", response_model=list[Location], summary="Get villages in a parish")
async def get_villages(parish_id: str):
    parish = uga_locale.find_parish_by_id(parish_id)
    if not parish:
        raise HTTPException(status_code=404, detail=f"Parish with id '{parish_id}' not found")
    villages = uga_locale.get_villages(parish_id)
    if not villages:
        raise HTTPException(status_code=404, detail=f"No villages found for parish '{parish['name']}' (id: {parish_id})")
    return villages


# Endpoints
@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=3, seconds=60))],
)
async def signup(
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: EmailStr = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    profile_image: UploadFile | None = File(None),
    region: str | None = Form(None),
    district_id: str | None = Form(None),
    county_id: str | None = Form(None),
    occupation: str | None = Form(None),
    bio: str | None = Form(None),
    political_interest: str | None = Form(None),
    community_role: str | None = Form(None),
    interests: str | None = Form(None),  # JSON string expected from client
    privacy_level: str | None = Form("public"),
    remember_me: bool = Form(False),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    # Enforce password strength rules (length, complexity, denylist)
    try:
        validate_password_strength(password)
    except PasswordValidationError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc

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
        try:
            profile_image_url = await upload_to_cloudinary(profile_image, folder="civcon/profiles")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Image upload failed: {e!s}") from e

    user_create = UserCreate(
        first_name=first_name,
        last_name=last_name,
        email=email,
        password=password,
        confirm_password=confirm_password,
        region=region,
        district_id=district_id,
        county_id=county_id,
        occupation=occupation,
        bio=bio,
        political_interest=political_interest,
        community_role=community_role,
        interests=parsed_interests,
        privacy_level=privacy_level,
    )

    created = await create_user(db, user_create, profile_image_path=profile_image_url)

    # Immediately issue a token pair so signup is a one-step flow
    token_pair = await _issue_token_pair(db, created, remember_me=remember_me, request=request)
    log_event(
        "signup.success",
        user_id=created.id,
        email=created.email,
        ip=_ip(request),
    )
    return _session_response_with_cookies(token_pair)


@router.post(
    "/login",
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    remember_me: bool = Form(False),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Email + password login.

    `remember_me=true` extends the refresh token lifetime from 14 to 30 days
    so a returning user can stay signed in across browser restarts. The
    access token lifetime is unchanged.

    F-008: the response body contains only ``{user, expires_in}`` —
    tokens are issued as HttpOnly cookies on the response, not in the
    JSON body. XSS can no longer read the access or refresh token.
    """
    user = await authenticate_user(db, form_data.username, form_data.password)
    if not user:
        # Same response shape for "no such user" and "wrong password" — prevents
        # account enumeration via the login endpoint.
        log_event(
            "login.failed",
            reason="bad_credentials",
            email=form_data.username,
            ip=_ip(request),
            user_agent=(request.headers.get("user-agent") if request else None),
        )
        raise _credentials_error("Incorrect email or password")
    if not user.is_active:
        log_event(
            "login.suspended",
            level=logging.WARNING,
            user_id=user.id,
            email=user.email,
            ip=_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended or inactive",
        )

    token_pair = await _issue_token_pair(db, user, remember_me=remember_me, request=request)
    log_event(
        "login.success",
        user_id=user.id,
        email=user.email,
        ip=_ip(request),
        user_agent=(request.headers.get("user-agent") if request else None),
    )
    return _session_response_with_cookies(token_pair)


# ============================================================================
# Refresh token — rotate access token, reuse-detection revokes the family
# ============================================================================
@router.post(
    "/refresh",
    dependencies=[Depends(RateLimiter(times=60, seconds=60))],
)
async def refresh_token(
    request: Request,
    payload: RefreshTokenRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange a refresh token for a new access + refresh token pair.

    F-008: the refresh token is read from the ``civcon_refresh`` cookie
    first, and falls back to the JSON body for the transition window.
    A new pair of cookies is set on the response and the body is the
    safe ``AuthSessionResponse`` shape.

    Implements refresh-token rotation with reuse detection: if a previously
    rotated (and therefore revoked) refresh token is presented, the entire
    family is revoked and all sessions for that family are invalidated.
    """
    presented_refresh = read_refresh_cookie(request.cookies)
    if not presented_refresh and payload and payload.refresh_token:
        presented_refresh = payload.refresh_token
    if not presented_refresh:
        log_event("refresh.failed", reason="missing_token", ip=_ip(request))
        raise _credentials_error("Refresh token required")
    try:
        claims = decode_token(presented_refresh, expected_type=REFRESH_TOKEN_TYPE)
    except JWTError as exc:
        log_event("refresh.failed", reason="invalid_token", ip=_ip(request))
        raise _credentials_error(str(exc) or "Invalid refresh token") from exc

    email = claims.get("sub")
    family_id = claims.get("family")
    presented_jti = claims.get("jti")
    if not email or not family_id or not presented_jti:
        log_event("refresh.failed", reason="malformed", ip=_ip(request))
        raise _credentials_error("Malformed refresh token")

    user = await get_user_by_email(db, email)
    if not user or not user.is_active:
        log_event("refresh.failed", reason="user_not_found", email=email, ip=_ip(request))
        raise _credentials_error("User not found or inactive")

    # Look up the session row by BOTH family_id and current_jti. A user can
    # have many active sessions, so matching on family_id alone is
    # ambiguous and returns an arbitrary first row. The precise match is
    # also what makes reuse-detection correct: only the family row whose
    # `current_jti` matches the presented `jti` is a legitimate use.
    result = await db.execute(
        select(AuthSession).where(
            AuthSession.family_id == family_id,
            AuthSession.current_jti == presented_jti,
        )
    )
    session = result.scalar_one_or_none()

    if session is None or session.revoked:
        # Possible token theft — revoke every session in the family.
        await _revoke_family(db, family_id, reason="reuse_detected")
        log_event(
            "refresh.reuse_detected",
            level=logging.WARNING,
            user_id=user.id,
            email=email,
            family_id=family_id,
            reason="session_missing_or_revoked",
            ip=_ip(request),
        )
        raise _credentials_error("Refresh token reuse detected; please sign in again.")

    if session.current_jti != presented_jti:
        # Belt-and-braces: with the precise query above this branch
        # is unreachable, but keep the check so the invariant is
        # locally obvious to readers.
        await _revoke_family(db, family_id, reason="reuse_detected")
        log_event(
            "refresh.reuse_detected",
            level=logging.WARNING,
            user_id=user.id,
            email=email,
            family_id=family_id,
            reason="jti_mismatch",
            ip=_ip(request),
        )
        raise _credentials_error("Refresh token reuse detected; please sign in again.")

    # Rotate: mint a new refresh token in the same family, mark old jti as revoked
    new_refresh, new_refresh_exp, _ = create_refresh_token(
        user.email, family=family_id, remember_me=False
    )
    new_refresh_claims = decode_token(new_refresh)
    new_jti = new_refresh_claims["jti"]

    session.current_jti = new_jti
    session.expires_at = new_refresh_exp
    db.add(session)
    await db.commit()

    # Defence-in-depth: blacklist the *old* refresh token in Redis for the
    # remainder of its lifetime. The DB row flip (current_jti -> new_jti)
    # alone is enough to stop reuse, but if a stolen token is replayed
    # _before_ the DB write commits, the blacklist short-circuits it.
    try:
        old_exp = int(claims.get("exp") or 0)
        ttl = old_exp - int(datetime.now(tz=UTC).timestamp())
        if ttl > 0:
            r = await get_redis()
            await r.setex(f"blacklist:{presented_refresh}", ttl, "true")
    except Exception:
        # Blacklist is best-effort — never block a successful refresh.
        pass

    # Issue access token
    access_token, access_exp = create_access_token(
        {"sub": user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        token_type=ACCESS_TOKEN_TYPE,
    )

    log_event(
        "refresh.success",
        user_id=user.id,
        email=user.email,
        family_id=family_id,
        ip=_ip(request),
    )

    token_pair = Token(
        access_token=access_token,
        refresh_token=new_refresh,
        token_type="bearer",
        expires_in=int((access_exp - datetime.now(tz=UTC)).total_seconds()),
        user=UserOut.model_validate(user),
    )
    return _session_response_with_cookies(token_pair)


async def _revoke_family(db: AsyncSession, family_id: str, reason: str = "user_logout") -> None:
    """Revoke every session row matching the family_id."""
    result = await db.execute(
        select(AuthSession).where(AuthSession.family_id == family_id, AuthSession.revoked == False)
    )
    for row in result.scalars().all():
        row.revoked = True
        row.revoked_reason = reason
    await db.commit()


# ============================================================================
# Session listing / management
# ============================================================================
@router.get("/sessions", response_model=list[dict])
async def list_sessions(
    current_user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the user's currently active (non-revoked, non-expired) sessions."""
    now = datetime.now(tz=UTC)
    result = await db.execute(
        select(AuthSession)
        .where(
            AuthSession.user_id == current_user.id,
            AuthSession.revoked == False,
            AuthSession.expires_at > now,
        )
        .order_by(AuthSession.last_used_at.desc())
    )
    return [
        {
            "family_id": s.family_id,
            "user_agent": s.user_agent,
            "ip_address": s.ip_address,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "last_used_at": s.last_used_at.isoformat() if s.last_used_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
        }
        for s in result.scalars().all()
    ]


@router.post("/sessions/{family_id}/revoke")
async def revoke_session(
    family_id: str,
    current_user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a specific session by family id. The user can only revoke their own sessions."""
    result = await db.execute(
        select(AuthSession).where(
            AuthSession.family_id == family_id,
            AuthSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.revoked:
        return {"message": "Session already revoked"}
    session.revoked = True
    session.revoked_reason = "user_revoked"
    db.add(session)
    await db.commit()
    return {"message": "Session revoked"}


# Forgot password (sends token) - actual sending via FastMail or an external service
@router.post(
    "/forgot-password",
    dependencies=[Depends(RateLimiter(times=3, seconds=3600))],
)
async def forgot_password(request: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    email = request.email
    user = await get_user_by_email(db, email)

    # Always return the same response — prevents account enumeration.
    generic_response = {"message": "If that email exists, a reset link was sent."}

    if not user:
        return generic_response

    # Single-purpose, short-lived (30 min) token carrying the password-reset
    # scope. Decoders elsewhere require the matching `expected_type` to accept
    # it, so this cannot be confused for an access or refresh token.
    reset_token, _ = create_access_token(
        {"sub": email, "scope": PASSWORD_RESET_SCOPE},
        expires_delta=timedelta(minutes=30),
        token_type=PASSWORD_RESET_SCOPE,
    )
    reset_link = f"{settings.frontend_url.rstrip('/')}/reset-password?token={reset_token}"

    try:
        await send_reset_email(email, reset_link)
    except Exception as exc:
        # Don't leak the failure to the caller (would help enumeration)
        log_event(
            "password_reset.email_failed",
            level=logging.WARNING,
            email=email,
            reason=str(exc),
        )

    log_event("password_reset.requested", email=email)
    return generic_response


@router.post(
    "/reset-password",
    dependencies=[Depends(RateLimiter(times=5, seconds=3600))],
)
async def reset_password(
    data: ResetPasswordSchema = Body(...),
    db: AsyncSession = Depends(get_db),
):
    token = data.token
    new_password = data.new_password

    try:
        payload = decode_token(token, expected_type=PASSWORD_RESET_SCOPE)
    except JWTError as exc:
        raise HTTPException(status_code=400, detail="Invalid or expired token") from exc

    if payload.get("scope") != PASSWORD_RESET_SCOPE:
        raise HTTPException(status_code=400, detail="Invalid token scope")

    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=400, detail="Invalid token payload")

    user = await get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Validate the new password against the same strength rules
    try:
        validate_password_strength(new_password)
    except PasswordValidationError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc

    user.hashed_password = get_password_hash(new_password)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Revoke every active session for this user — the password may have been
    # compromised, so all live tokens must be invalidated.
    now = datetime.now(tz=UTC)
    result = await db.execute(
        select(AuthSession).where(
            AuthSession.user_id == user.id,
            AuthSession.revoked == False,
            AuthSession.expires_at > now,
        )
    )
    for s in result.scalars().all():
        s.revoked = True
        s.revoked_reason = "password_reset"
    await db.commit()

    log_event(
        "password_reset.completed",
        user_id=user.id,
        email=user.email,
    )
    return {"message": "Password reset successful"}


# Logout => blacklist current access token AND revoke the session row
# matching the optionally-present refresh token.
@router.post(
    "/logout",
    dependencies=[Depends(RateLimiter(times=30, seconds=60))],
)
async def logout(
    request: Request,
    token: str = Depends(access_token_from_cookie_or_header),
    refresh_token_header: str | None = Header(None, alias="Refresh-Token"),
    db: AsyncSession = Depends(get_db),
):
    """
    Invalidate the current access token AND the matching session row.

    F-008: the refresh token is read from the ``civcon_refresh`` cookie
    first, with a fallback to the legacy ``Refresh-Token`` header. The
    response clears all auth + CSRF cookies on the client. The access
    token is added to the Redis blacklist for the remainder of its
    lifetime and the matching ``Session`` row is marked ``revoked``.

    The previous implementation iterated every non-revoked session for
    the user trying to match `current_jti` (a refresh-token jti) against
    `payload["jti"]` (an access-token jti). They never match, so the DB
    revocation silently no-op'd for every real user.
    """
    try:
        payload = decode_token(token, expected_type=ACCESS_TOKEN_TYPE)
    except JWTError:
        # Even on a bad token we still clear the cookies — the client
        # wants the local state gone regardless. But we DO need to know
        # whether we have a usable session row to revoke, so we
        # fall through and try to find a session by the refresh cookie.
        payload = {}

    # Resolve the refresh token from the cookie (preferred) or the
    # legacy header (transition).
    presented_refresh = read_refresh_cookie(request.cookies)
    if not presented_refresh and refresh_token_header:
        presented_refresh = refresh_token_header

    exp = payload.get("exp")
    if exp:
        ttl = int(exp - datetime.now(tz=UTC).timestamp())
        if ttl > 0:
            r = await get_redis()
            await r.setex(f"blacklist:{token}", ttl, "true")

    email = payload.get("sub")
    user_id: int | None = None
    if email:
        user = await get_user_by_email(db, email)
        if user:
            user_id = user.id
            revoked_family_id: str | None = None
            if presented_refresh:
                # Decode the presented refresh token and revoke the
                # matching session row only.
                try:
                    refresh_claims = decode_token(
                        presented_refresh, expected_type=REFRESH_TOKEN_TYPE
                    )
                    family_id = refresh_claims.get("family")
                    presented_jti = refresh_claims.get("jti")
                    if family_id and presented_jti:
                        result = await db.execute(
                            select(AuthSession).where(
                                AuthSession.family_id == family_id,
                                AuthSession.current_jti == presented_jti,
                                AuthSession.user_id == user.id,
                            )
                        )
                        session = result.scalar_one_or_none()
                        if session and not session.revoked:
                            session.revoked = True
                            session.revoked_reason = "logout"
                            await db.commit()
                            revoked_family_id = family_id
                except JWTError:
                    # Refresh token is unparseable — fall through and
                    # log without an exception. We don't 4xx the logout
                    # because the access token has already been
                    # blacklisted and the client is going to discard it.
                    pass

            log_event(
                "logout.success",
                user_id=user_id,
                email=email,
                family_id=revoked_family_id,
                ip=_ip(request),
                user_agent=(request.headers.get("user-agent") if request else None),
            )

    response = JSONResponse(
        status_code=200,
        content={"message": "Logged out"},
    )
    clear_auth_cookies(response)
    return response


#  protected endpoint
@router.get("/me", response_model=UserOut)
async def me(user: UserOut = Depends(get_current_user)):
    return user


# ============================================================================
# OAuth bootstrap exchange
# ============================================================================
# The OAuth callbacks (Google, LinkedIn) mint a single-use 60-second
# bootstrap code, store the Token payload in Redis, and redirect to
# `/social-login?code=<code>`. The SPA then POSTs the code here to
# swap it for the actual Token.
@router.post(
    "/oauth/exchange",
    dependencies=[Depends(RateLimiter(times=30, seconds=60))],
)
async def oauth_exchange(
    data: OAuthBootstrapExchangeRequest,
    request: Request,
):
    """
    Exchange a one-time bootstrap code for a full session.

    The code is deleted on read (atomic `GETDEL`); replays return 401.
    F-008: the access and refresh tokens are set on the response as
    HttpOnly cookies; the body is the safe ``AuthSessionResponse`` shape.
    """
    if not data.code or len(data.code) > 256:
        # Defensive: never let an arbitrary long string hit Redis.
        log_event(
            "oauth.exchange.failed",
            reason="invalid_code",
            ip=_ip(request),
        )
        raise HTTPException(status_code=400, detail="Invalid bootstrap code")

    payload = await _consume_bootstrap_code(data.code)
    if payload is None:
        log_event(
            "oauth.exchange.failed",
            reason="expired_or_used",
            ip=_ip(request),
        )
        raise _credentials_error("OAuth code expired or already used")

    log_event("oauth.exchange.success", ip=_ip(request))

    token_pair = Token(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        token_type="bearer",
        expires_in=int(payload["expires_in"]),
        user=UserOut.model_validate(payload["user"]),
    )
    return _session_response_with_cookies(token_pair)



#  GOOGLE
@router.get("/google/login")
async def google_login(request: Request):
    """
    SECURITY (F-005): generate an explicit, cryptographically random
    `state` parameter and bind it to the user's session cookie. The
    callback (`/google/callback`) verifies the state matches before
    consuming the OAuth code. This closes the residual CSRF risk that
    would otherwise rely on authlib's implicit session-cookie `state`
    alone (authlib's default is sound, but pairing it with an explicit,
    auditable value gives us logs and lets the SPA cross-check if it
    wants to).
    """
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_provider"] = "google"
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI") or f"{settings.backend_url.rstrip('/')}/auth/google/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri, state=state)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    next: str | None = Query(None),
):
    """
    Google OAuth callback. Issues a full access + refresh token pair, then
    redirects to the SPA with both tokens in the query string. The SPA
    picks them up, stores them, and discards the URL.
    """
    # SECURITY (F-005): verify the explicit state parameter. If the
    # session-stored value is missing or does not match the state we
    # sent to the provider, treat this as a forged callback and 400.
    expected_state = request.session.pop("oauth_state", None)
    request.session.pop("oauth_provider", None)
    received_state = request.query_params.get("state")
    if not expected_state or not received_state or expected_state != received_state:
        raise HTTPException(
            status_code=400,
            detail="OAuth state mismatch — possible CSRF, refusing to proceed.",
        )
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Google authorization failed: {exc}") from exc

    user_info = token.get("userinfo") or {}
    email = user_info.get("email")
    name = user_info.get("name", "")
    picture = user_info.get("picture", "")

    if not email:
        raise HTTPException(status_code=400, detail="Email not found in Google response")

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        first_name, last_name = _split_full_name(name)
        username = await _ensure_unique_username(db, email)
        sub = user_info.get("sub")
        user = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
            username=username,
            fullname=name,
            profile_image=picture,
            is_active=True,
            provider="google",
            google_id=sub,
            oauth_sub=sub,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        # Existing email/password user logging in via Google for the first
        # time — record the provider + sub so subsequent sign-ins are
        # deterministic. Do NOT silently merge roles or passwords; the
        # explicit linking flow is `POST /auth/link/{provider}`.
        sub = user_info.get("sub")
        if sub and user.provider != "google":
            user.provider = "google"
        if sub and not user.google_id:
            user.google_id = sub
        if sub and not user.oauth_sub:
            user.oauth_sub = sub
        if picture and not user.profile_image:
            user.profile_image = picture
        await db.commit()
        await db.refresh(user)

    token_pair = await _issue_token_pair(db, user, remember_me=False, request=request)

    # F-008: set the HttpOnly auth + CSRF cookies on the redirect
    # response, AND keep the bootstrap-code handoff for clients that
    # haven't been migrated yet. The cookies land on the browser before
    # the redirect to the SPA is followed, so the SPA can rely on
    # cookie-based auth immediately.
    target = next or settings.frontend_url.rstrip("/") + "/social-login"
    from urllib.parse import urlencode

    code = await _mint_bootstrap_code(token_pair)
    log_event(
        "oauth.bootstrap_issued",
        user_id=user.id,
        email=user.email,
        provider="google",
        ip=_ip(request),
    )
    qs = urlencode({"code": code})
    response = RedirectResponse(url=f"{target}?{qs}")
    set_auth_cookies(
        response,
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token or "",
        remember_me=False,
    )
    set_csrf_cookie(response)
    return response


#  LINKEDIN
@router.get("/linkedin/login")
async def linkedin_login(request: Request):
    """
    SECURITY (F-005): same explicit-state pattern as Google — see the
    comment on `google_login`.
    """
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_provider"] = "linkedin"
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI") or f"{settings.backend_url.rstrip('/')}/auth/linkedin/callback"
    return await oauth.linkedin.authorize_redirect(request, redirect_uri, state=state)


@router.get("/linkedin/callback")
async def linkedin_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    next: str | None = Query(None),
):
    """
    LinkedIn OAuth callback. Mirrors /auth/google/callback.

    With LinkedIn's OpenID metadata in play, `authorize_access_token` now
    populates `userinfo` automatically, so the callback reduces to the
    same shape as Google (no separate /me + /emailAddress fetches).
    """
    # SECURITY (F-005): explicit state verification. See the comment
    # on `google_callback` for the rationale.
    expected_state = request.session.pop("oauth_state", None)
    request.session.pop("oauth_provider", None)
    received_state = request.query_params.get("state")
    if not expected_state or not received_state or expected_state != received_state:
        raise HTTPException(
            status_code=400,
            detail="OAuth state mismatch — possible CSRF, refusing to proceed.",
        )
    try:
        token = await oauth.linkedin.authorize_access_token(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"LinkedIn authorization failed: {exc}") from exc

    user_info = token.get("userinfo") or {}
    email = user_info.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="LinkedIn did not return an email")

    name = user_info.get("name") or ""
    picture = user_info.get("picture") or None

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        first_name, last_name = _split_full_name(name)
        username = await _ensure_unique_username(db, email)
        sub = user_info.get("sub")
        user = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
            username=username,
            fullname=name,
            profile_image=picture,
            is_active=True,
            provider="linkedin",
            linkedin_id=sub,
            oauth_sub=sub,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        sub = user_info.get("sub")
        if sub and user.provider != "linkedin":
            user.provider = "linkedin"
        if sub and not user.linkedin_id:
            user.linkedin_id = sub
        if sub and not user.oauth_sub:
            user.oauth_sub = sub
        if picture and not user.profile_image:
            user.profile_image = picture
        await db.commit()
        await db.refresh(user)

    token_pair = await _issue_token_pair(db, user, remember_me=False, request=request)

    # F-008: set the HttpOnly auth + CSRF cookies on the redirect
    # response, AND keep the bootstrap-code handoff for clients that
    # haven't been migrated yet. See Google callback for the rationale.
    target = next or settings.frontend_url.rstrip("/") + "/social-login"
    from urllib.parse import urlencode

    code = await _mint_bootstrap_code(token_pair)
    log_event(
        "oauth.bootstrap_issued",
        user_id=user.id,
        email=user.email,
        provider="linkedin",
        ip=_ip(request),
    )
    qs = urlencode({"code": code})
    response = RedirectResponse(url=f"{target}?{qs}")
    set_auth_cookies(
        response,
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token or "",
        remember_me=False,
    )
    set_csrf_cookie(response)
    return response



@router.get("/check-username")
async def check_username(
    username: str = Query(..., min_length=3, max_length=30),
    db: AsyncSession = Depends(get_db),
):
    """
     Asynchronously checks if a username is already taken.
    Returns:
        {"available": True} if username is free
        {"available": False} if it's already taken
    """
    username = username.strip().lower()

    stmt = select(User).where(User.username == username)
    result = await db.execute(stmt)
    user = result.scalars().first()

    return {"available": user is None}