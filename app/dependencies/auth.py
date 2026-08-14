"""
Centralised authentication dependencies.

This module is the single source of truth for every dependency that a route
uses to authenticate or authorise a request:

- `oauth2_scheme`                — the FastAPI security scheme (`Bearer`).
- `get_current_user`             — full validation: signature, expiry,
                                    token type, Redis blacklist, session
                                    row, and `is_active`.
- `get_current_active_user`      — `get_current_user` + `is_active` guard
                                    for routes that just need a live user.
- `require_role(*roles)`         — dependency factory: only allows the
                                    listed roles (ADMIN always passes).
- `require_admin`                — short-hand for `Role.ADMIN`.
- `require_admin_or_self`        — admin OR the resource's owner.

Every router MUST import from this module instead of defining its own
dependency. The previous `app/routers/oauth2.py` duplicated
`get_current_user` with a weaker implementation that skipped the type,
blacklist, and session checks — every protected route was effectively
running with weaker security than intended. Centralising here closes that
gap.
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth import (
    ACCESS_TOKEN_TYPE,
    decode_token,
)
from app.core.cookies import read_access_cookie
from app.crud import get_user_by_email
from app.database import get_db
from app.models import User
from app.redis_client import get_redis
from app.schemas import Role, UserOut

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security scheme
# ---------------------------------------------------------------------------
# Security scheme
# ---------------------------------------------------------------------------
# Single scheme instance shared across the app. FastAPI uses the
# `tokenUrl` purely for OpenAPI docs; the actual login endpoint is
# `/auth/login` (form-encoded).
#
# This scheme is used by the legacy `Authorization: Bearer` fallback
# path. As of F-008, the preferred path is the `civcon_access`
# HttpOnly cookie. The dependency
# :func:`access_token_from_cookie_or_header` tries the cookie first and
# falls back to the header so the transition is seamless.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# ---------------------------------------------------------------------------
# Token source: cookie first, Authorization header second
# ---------------------------------------------------------------------------
async def access_token_from_cookie_or_header(
    request: Request,
    legacy_token: str | None = Depends(oauth2_scheme),
) -> str:
    """Resolve the access token for the current request.

    Order of preference:
      1. ``civcon_access`` HttpOnly cookie (the new F-008 path).
      2. ``Authorization: Bearer <token>`` header (the legacy path;
         still accepted during the transition window so existing test
         fixtures and tools keep working).

    Raises 401 with the standard error envelope when neither is present.
    """
    token = read_access_cookie(request.cookies)
    if token:
        return token
    if legacy_token:
        return legacy_token
    raise _credentials_error("Authentication required")


# ---------------------------------------------------------------------------
# Per-request user cache
# ---------------------------------------------------------------------------
# Admin pages fan out to several endpoints in a single render. Without a
# per-request memo the same email is looked up 5-10 times per page load,
# each round-trip paying the full DB + Redis round-trip cost. We key on
# the request itself (no global cache) so two concurrent requests still
# see fresh state.
import contextvars

_request_user_cache: contextvars.ContextVar[dict[str, User]] = contextvars.ContextVar(
    "request_user_cache", default={}  # noqa: B039 — `default={}` is the standard ContextVar pattern
)


def _get_cached_user(token: str) -> User | None:
    cache = _request_user_cache.get()
    return cache.get(token)


def _set_cached_user(token: str, user: User) -> None:
    cache = dict(_request_user_cache.get())
    cache[token] = user
    _request_user_cache.set(cache)


def clear_request_user_cache() -> None:
    """Reset the per-request cache. Called by tests; production code can ignore."""
    _request_user_cache.set({})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _credentials_error(
    detail: str = "Could not validate credentials",
) -> HTTPException:
    """Return a 401 with the standard `WWW-Authenticate` error fields."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={
            "WWW-Authenticate": f'Bearer error="invalid_token", error_description="{detail}"'
        },
    )


async def _is_blacklisted(token: str) -> bool:
    """Check the Redis blacklist for a revoked access token.

    **Fail-closed**: if Redis is unavailable, we treat the token
    as revoked. The blacklist is a security control, not an
    availability feature — the alternative (fail-open) lets a
    leaked-but-revoked token through during a Redis outage, which
    defeats the point of the blacklist.

    A `log.error` is emitted so the operator can see the outage
    in their alerting.
    """
    try:
        redis = await get_redis()
    except Exception as exc:
        logger.error(
            "Blacklist check failed (Redis unreachable, failing closed): %s",
            exc,
        )
        return True
    try:
        return bool(await redis.get(f"blacklist:{token}"))
    except Exception as exc:
        logger.error(
            "Blacklist check failed (Redis error, failing closed): %s", exc,
        )
        return True


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
async def get_current_user(
    token: str = Depends(access_token_from_cookie_or_header),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Validate an access token and return the current `User`.

    Performs (in order):
      1. Blacklist check (Redis)
      2. Decode + verify signature, expiry, and `type=access`
      3. Look up the user by `sub` (email) — cached per-request so
         admin pages that fan out to multiple endpoints don't re-query
         the DB on each one.
      4. Ensure the user exists and `is_active=True`

    Returns the SQLAlchemy `User` model so callers can access relationship
    attributes. Use `UserOut.model_validate(...)` when serialising.

    The token is read from the ``civcon_access`` cookie if present, with
    a fallback to the ``Authorization: Bearer`` header for the
    transition window. See :func:`access_token_from_cookie_or_header`.
    """
    # Fast path: this token was already validated in the same request.
    # Bypasses Redis blacklist + JWT decode + DB lookup — admin pages
    # typically fan out to 4-8 endpoints, so this saves 3-7 round-trips
    # per page load.
    cached = _get_cached_user(token)
    if cached is not None:
        return cached

    if await _is_blacklisted(token):
        raise _credentials_error("Token revoked")

    try:
        payload = decode_token(token, expected_type=ACCESS_TOKEN_TYPE)
    except JWTError as exc:
        raise _credentials_error(str(exc) or "Could not validate credentials") from exc

    email: str | None = payload.get("sub")
    if not email:
        raise _credentials_error("Token missing subject")

    user = await get_user_by_email(db, email)
    if user is None:
        raise _credentials_error("User not found")
    if not user.is_active:
        # 403, not 401, so the client can distinguish "you're signed in
        # but suspended" from "your token is bad".
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended or inactive",
        )

    _set_cached_user(token, user)
    return user


async def get_current_active_user(
    user: User = Depends(get_current_user),
) -> UserOut:
    """`get_current_user` projected to `UserOut` (handy for route handlers)."""
    return UserOut.model_validate(user)


# ---------------------------------------------------------------------------
# Role-based access
# ---------------------------------------------------------------------------
def require_role(roles: list[Role]):
    """
    Factory returning an async dependency that checks the user's role.

    ADMIN always passes — there is no way for a route to exclude admins.
    Returns the SQLAlchemy `User` so callers that need to mutate the
    model (e.g. `current_user.is_active = False`) continue to work.
    """
    if not roles:
        raise ValueError("require_role() needs at least one role")

    allowed = {r.value for r in roles}

    async def dependency(user: User = Depends(get_current_user)) -> User:
        role_value = getattr(user.role, "value", user.role)
        if role_value == Role.ADMIN.value or role_value in allowed:
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Operation requires one of: {', '.join(sorted(allowed))}",
        )

    return dependency


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Single source of truth for "must be an admin"."""
    role_value = getattr(user.role, "value", user.role)
    if role_value != Role.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


async def require_admin_or_self(
    user_id: int,
    user: User = Depends(get_current_user),
) -> User:
    """Allow admins, or the user themselves to act on their own record."""
    role_value = getattr(user.role, "value", user.role)
    if role_value == Role.ADMIN.value:
        return user
    if user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation requires admin or the resource owner",
        )
    return user


# Re-export so existing imports of `ACCESS_TOKEN_TYPE`, `decode_token`,
# and `settings` continue to work via this module.
__all__ = [
    "ACCESS_TOKEN_TYPE",
    "access_token_from_cookie_or_header",
    "decode_token",
    "get_current_active_user",
    "get_current_user",
    "oauth2_scheme",
    "require_admin",
    "require_admin_or_self",
    "require_role",
    "settings",
]