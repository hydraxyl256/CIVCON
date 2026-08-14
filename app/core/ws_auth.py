"""
WebSocket authentication helpers.

The HTTP auth dependency (`get_current_user` in
`app.dependencies.auth`) is built around FastAPI's `Depends` —
it can't be reused directly inside `@app.websocket(...)` because
WebSocket query params aren't surfaced the same way. This module
provides:

- `WS_CLOSE_CODES` — semantic close codes (4401/4403/1011) so
  the frontend can branch on a closed socket's `code` rather
  than parsing the (often empty) `reason` string.
- `authenticate_ws(token, db)` — runs the same validation as
  `get_current_user` (blacklist + signature + expiry + active)
  but takes the token directly, raising `WebSocketAuthError`
  on failure. The caller decides whether to close the socket
  or return a 4xxx code.
"""
from __future__ import annotations

from fastapi import WebSocket
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import ACCESS_TOKEN_TYPE, decode_token
from app.core.cookies import ACCESS_COOKIE
from app.crud import get_user_by_email
from app.models import User

# Semantic close codes. 4xxx is the application-defined range
# (RFC 6455 reserves 4000-4999 for private use); we use the same
# code for both missing-token and invalid-token because the
# frontend's response (re-prompt for login) is the same.
WS_CLOSE_CODES = {
    "missing_token": 4401,
    "invalid_token": 4401,
    "forbidden": 4403,
    "internal_error": 1011,
}


class WebSocketAuthError(Exception):
    """Raised by `authenticate_ws` when a token is missing,
    malformed, expired, or revoked."""

    def __init__(self, reason: str, code: str = "invalid_token") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


async def authenticate_ws(
    token: str | None,
    db: AsyncSession,
    cookies: dict | None = None,
) -> User:
    """Validate a WebSocket bearer token.

    Token source (F-008): prefer the ``civcon_access`` HttpOnly cookie
    (so the URL no longer needs to carry the JWT) and fall back to a
    ``?token=`` query param for legacy clients that haven't been
    migrated. The caller is responsible for passing the parsed cookies
    mapping from the WebSocket scope (``websocket.cookies`` or
    ``websocket.scope['cookies']``); the legacy token arg is still
    accepted so existing routers keep working.

    Performs (in order):
      1. Token presence (cookie first, then query)
      2. Blacklist check (Redis) — falls closed on outage
         (see `get_current_user`)
      3. JWT signature, expiry, and `type=access`
      4. User lookup by `sub` (email)
      5. `is_active` check (returns WebSocketAuthError rather
         than HTTPException since this is a WS handshake)
    """
    effective_token = token
    if cookies:
        # Starlette's ``WebSocket.cookies`` is a dict-like view.
        try:
            cookie_token = cookies.get(ACCESS_COOKIE)
        except Exception:
            cookie_token = None
        if cookie_token:
            effective_token = cookie_token
    if not effective_token:
        raise WebSocketAuthError("Missing token", code="missing_token")
    from app.dependencies.auth import _is_blacklisted  # local import to avoid cycle
    if await _is_blacklisted(effective_token):
        raise WebSocketAuthError("Token revoked", code="invalid_token")
    try:
        payload = decode_token(effective_token, expected_type=ACCESS_TOKEN_TYPE)
    except JWTError as exc:
        raise WebSocketAuthError(str(exc) or "Could not validate credentials") from exc
    email = payload.get("sub")
    if not email:
        raise WebSocketAuthError("Token missing subject")
    user = await get_user_by_email(db, email)
    if user is None:
        raise WebSocketAuthError("User not found")
    if not user.is_active:
        # 4403: account suspended or inactive.
        raise WebSocketAuthError("Account is suspended or inactive", code="forbidden")
    return user


async def close_ws_with_auth_error(websocket: WebSocket, err: WebSocketAuthError) -> None:
    """Close a WebSocket with the appropriate semantic close code."""
    code = WS_CLOSE_CODES.get(err.code, WS_CLOSE_CODES["invalid_token"])
    try:
        await websocket.close(code=code, reason=err.reason[:120])
    except Exception:
        pass
