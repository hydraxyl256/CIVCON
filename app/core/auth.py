"""
Centralised authentication helpers.

This module is the single source of truth for:

- JWT encoding / decoding (with explicit `type` and `family` claims)
- Token family tracking (refresh-token rotation with reuse detection)
- Password hashing (bcrypt via passlib)
- Password strength validation
- Common-password denylist

All other modules should import from here instead of duplicating the
create_access_token / decode logic.
"""
from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

# ============================================================================
# Password hashing
# ============================================================================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        return False


# ============================================================================
# Password strength validation
# ============================================================================
# A small denylist of the most common passwords — chosen because they appear
# in nearly every breach corpus and consistently account for > 25 % of all
# compromised credentials in published telemetry.
COMMON_PASSWORDS = {
    "123456", "password", "12345678", "qwerty", "111111", "12345",
    "123456789", "1234567", "1234", "iloveyou", "admin", "welcome",
    "monkey", "login", "abc123", "starwars", "letmein", "dragon",
    "master", "hello", "freedom", "whatever", "passw0rd", "trustno1",
    "000000", "1q2w3e4r", "qwerty123", "password1", "uganda",
}


class PasswordValidationError(ValueError):
    """Raised when a password fails one of the strength rules."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def validate_password_strength(password: str) -> None:
    """
    Enforce minimum length, mixed case, digit, and a basic denylist.

    Raises PasswordValidationError with a stable error code on failure.
    """
    if not password:
        raise PasswordValidationError("password_required", "Password is required.")
    if len(password) < 8:
        raise PasswordValidationError(
            "password_too_short", "Password must be at least 8 characters long."
        )
    if len(password) > 128:
        raise PasswordValidationError(
            "password_too_long", "Password must be at most 128 characters long."
        )
    if password.lower() in COMMON_PASSWORDS:
        raise PasswordValidationError(
            "password_too_common", "This password is too common. Please choose another."
        )
    if not re.search(r"[a-z]", password):
        raise PasswordValidationError(
            "password_missing_lowercase", "Password must contain at least one lowercase letter."
        )
    if not re.search(r"[A-Z]", password):
        raise PasswordValidationError(
            "password_missing_uppercase", "Password must contain at least one uppercase letter."
        )
    if not re.search(r"\d", password):
        raise PasswordValidationError(
            "password_missing_digit", "Password must contain at least one digit."
        )


# ============================================================================
# JWT helpers
# ============================================================================
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
PASSWORD_RESET_SCOPE = "password_reset"
EMAIL_VERIFY_SCOPE = "email_verify"

# Default lifetimes — can be overridden at call site.
DEFAULT_ACCESS_TOKEN_TTL_MINUTES = 60
DEFAULT_REFRESH_TOKEN_TTL_DAYS = 14
REMEMBER_ME_REFRESH_TOKEN_TTL_DAYS = 30


def _utcnow() -> datetime:
    """Timezone-aware UTC now (Python 3.12+ deprecates naive utcnow)."""
    return datetime.now(tz=UTC)


def create_access_token(
    data: dict,
    expires_delta: timedelta | None = None,
    token_type: str = ACCESS_TOKEN_TYPE,
    family: str | None = None,
) -> tuple[str, datetime]:
    """
    Encode and sign a JWT.

    Returns a tuple of (encoded_jwt, expires_at). Every token carries:
      - sub         : subject identifier (user email for access/refresh, jti for password-reset)
      - type        : 'access' | 'refresh' | 'password_reset' | 'email_verify'
      - jti         : unique token id (for revocation tracking)
      - family      : refresh-token family id (refresh tokens only; reused tokens revoke the family)
      - iat         : issued-at
      - exp         : expiry
    """
    to_encode = data.copy()
    expire = _utcnow() + (
        expires_delta
        if expires_delta
        else timedelta(minutes=DEFAULT_ACCESS_TOKEN_TTL_MINUTES)
    )
    to_encode.setdefault("jti", uuid.uuid4().hex)
    to_encode.update(
        {
            "type": token_type,
            "iat": int(_utcnow().timestamp()),
            "exp": expire,
        }
    )
    if family:
        to_encode["family"] = family
    encoded = jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)
    return encoded, expire


def create_refresh_token(
    email: str, family: str | None = None, remember_me: bool = False
) -> tuple[str, datetime, str]:
    """
    Create a refresh token. If `family` is provided, the new token is part of
    the same login session; otherwise a new family is generated. Returns
    (token, expires_at, family_id).
    """
    family_id = family or uuid.uuid4().hex
    ttl_days = (
        REMEMBER_ME_REFRESH_TOKEN_TTL_DAYS
        if remember_me
        else DEFAULT_REFRESH_TOKEN_TTL_DAYS
    )
    token, expires_at = create_access_token(
        {"sub": email},
        expires_delta=timedelta(days=ttl_days),
        token_type=REFRESH_TOKEN_TYPE,
        family=family_id,
    )
    return token, expires_at, family_id


def decode_token(token: str, expected_type: str | None = None) -> dict:
    """
    Decode and verify a JWT, optionally asserting its `type` claim.

    Raises jose.JWTError on any failure (signature, expiry, malformed, wrong type).
    """
    payload = jwt.decode(
        token, settings.secret_key, algorithms=[settings.algorithm]
    )
    if expected_type is not None and payload.get("type") != expected_type:
        raise JWTError(f"Invalid token type: expected {expected_type!r}")
    return payload


def token_expires_in(payload: dict) -> int:
    """Return the number of seconds until `exp`, clamped to >= 0."""
    exp = payload.get("exp")
    if not exp:
        return 0
    return max(0, int(exp - _utcnow().timestamp()))


def random_token(length: int = 32) -> str:
    """Cryptographically strong opaque token (e.g. for password-reset URL params)."""
    return secrets.token_urlsafe(length)
