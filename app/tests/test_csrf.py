"""
F-008 regression tests: double-submit cookie CSRF middleware.

The middleware enforces:
- GET / HEAD / OPTIONS are never checked.
- POST / PUT / PATCH / DELETE without `X-CSRF-Token` matching the
  `civcon_csrf` cookie returns 403.
- The exempt paths (first-contact auth + OAuth state callbacks) bypass
  the check.
- A mismatched header returns 403.
- A matching header lets the request through.

These tests assume a working Postgres + Redis are reachable via the
`DATABASE_URL` / `REDIS_URL` environment variables used by `app.config`,
because CSRF-protected endpoints reach into the DB / Redis.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.cookies import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    REFRESH_COOKIE,
)
from app.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _unique_email() -> str:
    return f"pytest_{uuid.uuid4().hex[:12]}@example.com"


@pytest_asyncio.fixture
async def logged_in_with_csrf(client):
    """Return a dict with the access cookie + a matching csrf cookie."""
    email = _unique_email()
    password = "Secret123Strong"
    signup = await client.post(
        "/auth/signup",
        data={
            "first_name": "Pytest",
            "last_name": "Csrf",
            "email": email,
            "password": password,
            "confirm_password": password,
        },
    )
    assert signup.status_code in (200, 201), signup.text

    csrf = signup.cookies.get(CSRF_COOKIE)
    access = signup.cookies.get(ACCESS_COOKIE)
    assert csrf, "signup did not set csrf cookie"
    assert access, "signup did not set access cookie"

    return {
        "email": email,
        "password": password,
        "csrf": csrf,
        "access": access,
        "cookies": {
            CSRF_COOKIE: csrf,
            ACCESS_COOKIE: access,
            REFRESH_COOKIE: signup.cookies.get(REFRESH_COOKIE),
        },
    }


# ---------------------------------------------------------------------------
# Middleware shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_csrf_missing_rejected(client, logged_in_with_csrf):
    """A POST to a CSRF-protected endpoint without X-CSRF-Token fails."""
    cookies = {
        ACCESS_COOKIE: logged_in_with_csrf["access"],
    }
    # POST to /auth/logout — state-changing and NOT in the exempt list.
    res = await client.post("/auth/logout", cookies=cookies)
    assert res.status_code == 403, res.text
    body = res.json()
    assert body.get("code") in ("csrf_missing", "csrf_failed"), body


@pytest.mark.asyncio
async def test_csrf_mismatch_rejected(client, logged_in_with_csrf):
    cookies = {
        ACCESS_COOKIE: logged_in_with_csrf["access"],
    }
    res = await client.post(
        "/auth/logout",
        cookies=cookies,
        headers={"X-CSRF-Token": "totally-wrong-token"},
    )
    assert res.status_code == 403, res.text
    body = res.json()
    assert body.get("code") == "csrf_failed", body


@pytest.mark.asyncio
async def test_csrf_match_passes(client, logged_in_with_csrf):
    """With a correct header + matching cookie, the logout endpoint
    goes through (returns 200)."""
    cookies = {
        ACCESS_COOKIE: logged_in_with_csrf["access"],
    }
    res = await client.post(
        "/auth/logout",
        cookies=cookies,
        headers={"X-CSRF-Token": logged_in_with_csrf["csrf"]},
    )
    assert res.status_code in (200, 204), res.text


@pytest.mark.asyncio
async def test_csrf_exempts_login(client):
    """POST /auth/login is in the exempt list — it doesn't need the
    CSRF header. We send NO cookies and expect a real 401 (wrong creds),
    not a 403 (CSRF)."""
    res = await client.post(
        "/auth/login",
        data={"username": "noone@example.com", "password": "x"},
    )
    # If CSRF were enforced here we'd see 403. We want the underlying
    # credential check to run instead.
    assert res.status_code == 401, res.text


@pytest.mark.asyncio
async def test_csrf_exempts_signup(client):
    """POST /auth/signup is also exempt."""
    res = await client.post(
        "/auth/signup",
        data={
            "first_name": "X",
            "last_name": "Y",
            "email": _unique_email(),
            "password": "Secret123Strong",
            "confirm_password": "Secret123Strong",
        },
    )
    # We expect a real auth outcome (success) rather than a CSRF 403.
    assert res.status_code in (200, 201), res.text


@pytest.mark.asyncio
async def test_csrf_accepts_xsrf_token_alias(client, logged_in_with_csrf):
    """The middleware also accepts the X-XSRF-Token header (axios's
    default name)."""
    cookies = {
        ACCESS_COOKIE: logged_in_with_csrf["access"],
    }
    res = await client.post(
        "/auth/logout",
        cookies=cookies,
        headers={"X-XSRF-Token": logged_in_with_csrf["csrf"]},
    )
    assert res.status_code in (200, 204), res.text


@pytest.mark.asyncio
async def test_csrf_get_does_not_require_header(client, logged_in_with_csrf):
    """GETs must never be blocked — /auth/me is a safe read."""
    cookies = {ACCESS_COOKIE: logged_in_with_csrf["access"]}
    res = await client.get("/auth/me", cookies=cookies)
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_csrf_refresh_requires_header(client, logged_in_with_csrf):
    """/auth/refresh is state-changing and NOT exempt — must enforce CSRF."""
    cookies = {
        REFRESH_COOKIE: logged_in_with_csrf["cookies"][REFRESH_COOKIE],
        ACCESS_COOKIE: logged_in_with_csrf["access"],
    }
    assert cookies[REFRESH_COOKIE]

    res = await client.post("/auth/refresh", cookies=cookies)
    assert res.status_code == 403, res.text
    body = res.json()
    assert body.get("code") in ("csrf_missing", "csrf_failed"), body
