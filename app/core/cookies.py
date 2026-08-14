"""
HttpOnly cookie helpers for the cookie-based session (F-008).

The pre-production audit flagged F-008 because the frontend stored JWT
access and refresh tokens in `localStorage`, which makes them readable
to any JavaScript running in the page (XSS → account takeover).

This module is the single place that builds the `Set-Cookie` headers so
the rest of the codebase doesn't need to know the cookie names, the
`HttpOnly`/`Secure`/`SameSite` attribute set, the path scoping, or the
remember-me vs. default `Max-Age`.

Cookie summary
--------------
- ``civcon_access``     HttpOnly; SameSite=Lax; Path=/;     Max-Age = access TTL.
- ``civcon_refresh``    HttpOnly; SameSite=Lax; Path=/auth; Max-Age = refresh TTL.
- ``civcon_csrf``       NOT HttpOnly (JS must read it); SameSite=Lax; Path=/.

The ``Secure`` flag is added in every non-development environment, mirroring
how ``SessionMiddleware`` is configured in ``app/main.py``.
"""
from __future__ import annotations

import secrets

from starlette.responses import Response

from app.config import settings

# ---------------------------------------------------------------------------
# Cookie names
# ---------------------------------------------------------------------------
ACCESS_COOKIE = "civcon_access"
REFRESH_COOKIE = "civcon_refresh"
CSRF_COOKIE = "civcon_csrf"

# Path scoping: the refresh cookie is only sent to the auth router so an
# exfiltration via a careless `fetch(..., credentials: "include")` from a
# non-auth route is impossible. The access cookie goes everywhere; the
# CSRF cookie is read by every state-changing request.
REFRESH_COOKIE_PATH = "/auth"

# Default TTLs (mirror the token TTLs from app.core.auth so the cookie
# cannot outlive the token it carries).
DEFAULT_ACCESS_TTL_SECONDS = 60 * 60              # 1 hour
DEFAULT_REFRESH_TTL_SECONDS = 14 * 24 * 60 * 60   # 14 days
REMEMBER_ME_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


def is_dev() -> bool:
    """Return True when running in development.

    Mirrors the gate that ``SessionMiddleware`` uses to decide whether to
    set the ``Secure`` cookie attribute. We keep them aligned so a single
    env-var flip toggles both.
    """
    return settings.environment.lower() == "development"


# ---------------------------------------------------------------------------
# Set / clear
# ---------------------------------------------------------------------------
def _secure_flag() -> bool:
    """Whether to attach the ``Secure`` attribute on cookies."""
    return not is_dev()


def set_access_cookie(
    response: Response,
    token: str,
    *,
    max_age_seconds: int = DEFAULT_ACCESS_TTL_SECONDS,
) -> None:
    """Attach the access-token cookie to ``response``."""
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=token,
        max_age=max_age_seconds,
        path="/",
        secure=_secure_flag(),
        httponly=True,
        samesite="lax",
    )


def set_refresh_cookie(
    response: Response,
    token: str,
    *,
    remember_me: bool = False,
) -> None:
    """Attach the refresh-token cookie to ``response``.

    The cookie is path-scoped to ``/auth`` so the browser only sends it
    to the auth router — minimising the surface for accidental leakage
    via unrelated endpoints.
    """
    max_age = (
        REMEMBER_ME_REFRESH_TTL_SECONDS if remember_me else DEFAULT_REFRESH_TTL_SECONDS
    )
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=max_age,
        path=REFRESH_COOKIE_PATH,
        secure=_secure_flag(),
        httponly=True,
        samesite="lax",
    )


def set_csrf_cookie(response: Response, token: str | None = None) -> str:
    """Attach the CSRF cookie to ``response``.

    The token is generated server-side (32 random bytes, url-safe). The
    cookie is NOT HttpOnly: the front-end must be able to read it so it
    can echo the value in the ``X-CSRF-Token`` request header.

    Returns the generated token so the caller can also surface it on
    the response body if desired (not currently used).
    """
    if token is None:
        token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        max_age=DEFAULT_REFRESH_TTL_SECONDS,  # outlives the access cookie
        path="/",
        secure=_secure_flag(),
        httponly=False,  # explicit: JS must read this
        samesite="lax",
    )
    return token


def set_auth_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    remember_me: bool = False,
) -> None:
    """Set the access + refresh cookies on ``response`` in one call.

    Callers usually want both: the access cookie authenticates the
    current request and the refresh cookie keeps the session alive for
    the next hour. CSRF is set separately so callers can rotate it
    independently (e.g. on login, on logout, periodically).
    """
    set_access_cookie(response, access_token)
    set_refresh_cookie(response, refresh_token, remember_me=remember_me)


def clear_auth_cookies(response: Response) -> None:
    """Expire both auth cookies on ``response``.

    Used on logout and on account-deletion flows. The browser drops
    the cookie when it sees ``Max-Age=0``. We pass the same
    ``Path``/``Secure``/``HttpOnly`` attributes as the original set so
    the browser is willing to overwrite the existing entry.
    """
    for name, path in (
        (ACCESS_COOKIE, "/"),
        (REFRESH_COOKIE, REFRESH_COOKIE_PATH),
    ):
        response.set_cookie(
            key=name,
            value="",
            max_age=0,
            path=path,
            secure=_secure_flag(),
            httponly=True,
            samesite="lax",
            expires=0,
        )
    # Also clear the CSRF cookie (sentinel — we don't strictly need to
    # do this on logout, but it's a clean signal that the session ended).
    response.set_cookie(
        key=CSRF_COOKIE,
        value="",
        max_age=0,
        path="/",
        secure=_secure_flag(),
        httponly=False,
        samesite="lax",
        expires=0,
    )


# ---------------------------------------------------------------------------
# Cookie -> token reads
# ---------------------------------------------------------------------------
def read_access_cookie(cookies) -> str | None:
    """Return the access token from a Starlette ``Cookies`` mapping, if any."""
    try:
        return cookies.get(ACCESS_COOKIE)
    except (AttributeError, KeyError, TypeError):
        # `cookies` may be None or a non-mapping in odd test scenarios;
        # any of these means we cannot read the cookie, so return None.
        return None


def read_refresh_cookie(cookies) -> str | None:
    """Return the refresh token from a Starlette ``Cookies`` mapping, if any."""
    try:
        return cookies.get(REFRESH_COOKIE)
    except (AttributeError, KeyError, TypeError):
        # `cookies` may be None or a non-mapping in odd test scenarios;
        # any of these means we cannot read the cookie, so return None.
        return None
