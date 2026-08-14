"""
CSRF protection middleware (F-008 follow-up).

Because authentication is now cookie-based, the browser will send the
session cookie automatically on every cross-origin request that includes
``credentials``. ``SameSite=Lax`` blocks the most obvious cross-site
attacks (top-level POST from another origin) but is not sufficient on
its own — the audit explicitly requires an explicit CSRF strategy.

This module implements the **double-submit cookie** pattern:
  * On login/signup/refresh, the server sets a non-HttpOnly cookie
    ``civcon_csrf`` containing a random token.
  * The front-end reads the cookie and echoes the token in the
    ``X-CSRF-Token`` request header.
  * For every state-changing request (``POST``/``PUT``/``PATCH``/
    ``DELETE``) the middleware requires that the header and the cookie
    match. We use ``hmac.compare_digest`` for the constant-time compare.

Exempt paths
------------
The following paths are first-contact or already protected by a stronger
mechanism (OAuth ``state`` for the OAuth callbacks) and do NOT need a
CSRF token:
  * ``/auth/login``         — first contact, no session yet
  * ``/auth/signup``        — first contact
  * ``/auth/forgot-password`` — first contact (no session)
  * ``/auth/reset-password``  — first contact (token in body)
  * ``/auth/oauth/*``        — already gated by OAuth ``state``

WebSocket upgrades (``scope['type'] != 'http'``) are passed through
unmodified — CSRF only applies to HTTP state-changing requests.
"""
from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.api_responses import ErrorResponse
from app.core.cookies import CSRF_COOKIE

logger = logging.getLogger("CIVCON.csrf")

# Methods that can mutate server state. GET/HEAD/OPTIONS are read-only
# and don't need a CSRF token.
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Header names accepted from the client. We accept both ``X-CSRF-Token``
# and ``X-XSRF-Token`` (the latter is what axios / angular defaults to
# when reading a non-HttpOnly cookie named ``XSRF-TOKEN``; we keep the
# flexibility even though our cookie name is different).
CSRF_HEADER_CANDIDATES = ("x-csrf-token", "x-xsrf-token")

# Path prefixes that are exempt from CSRF. Match is by startswith().
CSRF_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/auth/login",
    "/auth/signup",
    "/auth/forgot-password",
    "/auth/reset-password",
    "/auth/oauth/",
    "/auth/google/",
    "/auth/linkedin/",
    # /auth/refresh and /auth/logout are NOT exempt — the front-end
    # always sends the CSRF header for these. If the exemption list ever
    # grows, it must remain auditable.
)


def _is_exempt(path: str) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in CSRF_EXEMPT_PREFIXES)


class CsrfMiddleware(BaseHTTPMiddleware):
    """Reject HTTP state-changing requests missing a matching CSRF token.

    The middleware is intentionally minimal: read two headers + one
    cookie, do a constant-time compare, return 403 on failure. Anything
    more elaborate (origin checks, double-submit-with-jti, etc.) is
    out of scope for F-008.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Only HTTP scopes; pass WebSocket / lifespan / etc. through.
        if request.scope.get("type") != "http":
            return await call_next(request)

        # Only state-changing methods need a CSRF check.
        if request.method.upper() not in STATE_CHANGING_METHODS:
            return await call_next(request)

        # First-contact / OAuth flows are exempt.
        if _is_exempt(request.url.path):
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE)
        header_token: str | None = None
        for header_name in CSRF_HEADER_CANDIDATES:
            value = request.headers.get(header_name)
            if value:
                header_token = value
                break

        if not cookie_token or not header_token:
            logger.info(
                "csrf.rejected reason=%s path=%s method=%s",
                "missing" if not cookie_token else "header_missing",
                request.url.path,
                request.method,
            )
            return _csrf_error("csrf_missing")

        if not hmac.compare_digest(cookie_token, header_token):
            logger.info(
                "csrf.rejected reason=mismatch path=%s method=%s",
                request.url.path,
                request.method,
            )
            return _csrf_error("csrf_failed")

        return await call_next(request)


def _csrf_error(code: str) -> JSONResponse:
    """Build a 403 response in the standard error envelope."""
    detail = {
        "csrf_missing": (
            "CSRF token missing. Reload the page and try again — the "
            "session cookie and CSRF cookie must both be present."
        ),
        "csrf_failed": (
            "CSRF token did not match. Reload the page and try again."
        ),
    }.get(code, "CSRF check failed.")
    return JSONResponse(
        status_code=403,
        content=ErrorResponse(
            detail=detail,
            code=code,
        ).model_dump(),
    )
