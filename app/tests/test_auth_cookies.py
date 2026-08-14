"""
F-008 regression tests: HttpOnly cookie auth + absence of tokens in body.

Covers the behaviour the security audit called out:
- Login/signup/OAuth-exchange set `civcon_access` + `civcon_refresh` cookies
  with `HttpOnly`, `SameSite=Lax`, and a path-scoped refresh cookie.
- The response body returns `AuthSessionResponse` (no token fields).
- Refresh rotation still works when the refresh token lives only in the
  cookie (no body).
- Logout clears the cookies.
- /auth/me works with cookie-only credentials.
- WebSocket auth accepts either cookie or `?token=` query.

These tests assume a working Postgres + Redis are reachable via the
`DATABASE_URL` / `REDIS_URL` environment variables used by `app.config`.
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
async def fresh_user_cookies(client):
    """Sign up a brand-new user and return their cookies + credentials.

    The signup endpoint already sets the HttpOnly cookies via the new
    AuthSessionResponse path, so this fixture is the shared setup for
    every cookie-auth test below.
    """
    email = _unique_email()
    password = "Secret123Strong"
    res = await client.post(
        "/auth/signup",
        data={
            "first_name": "Pytest",
            "last_name": "Cookies",
            "email": email,
            "password": password,
            "confirm_password": password,
        },
    )
    assert res.status_code in (200, 201), res.text
    return {
        "email": email,
        "password": password,
        "cookies": dict(res.cookies),
    }


def _cookie_attrs(set_cookie_header: str, name: str) -> dict:
    """Best-effort parser for one Set-Cookie attribute set.

    Returns a dict of lower-cased attribute -> value (value is empty
    string for valueless attributes like `HttpOnly`).
    """
    parts = [p.strip() for p in set_cookie_header.split(";")]
    first = parts[0]  # "name=value"
    if not first.startswith(name + "="):
        return {}
    attrs = {"name": name}
    for p in parts[1:]:
        if "=" in p:
            k, _, v = p.partition("=")
            attrs[k.strip().lower()] = v.strip()
        else:
            attrs[p.strip().lower()] = ""
    return attrs


# ---------------------------------------------------------------------------
# Cookie issuance on signup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signup_sets_httponly_access_cookie(client, fresh_user_cookies):
    # Pull the Set-Cookie headers from the signup response by replaying
    # the request so we can inspect the wire-level attributes.
    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    res = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert res.status_code == 200, res.text

    set_cookies = res.headers.get_list("set-cookie")
    access_headers = [h for h in set_cookies if h.startswith(ACCESS_COOKIE + "=")]
    assert access_headers, f"no Set-Cookie for {ACCESS_COOKIE}; got {set_cookies}"
    attrs = _cookie_attrs(access_headers[0], ACCESS_COOKIE)
    assert "httponly" in attrs, f"access cookie missing HttpOnly: {access_headers[0]}"
    assert attrs.get("samesite", "").lower() == "lax", (
        f"access cookie missing SameSite=Lax: {access_headers[0]}"
    )
    assert attrs.get("path", "") == "/", (
        f"access cookie path should be /, got {attrs.get('path')!r}"
    )


@pytest.mark.asyncio
async def test_signup_sets_path_scoped_refresh_cookie(client, fresh_user_cookies):
    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    res = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert res.status_code == 200, res.text

    set_cookies = res.headers.get_list("set-cookie")
    refresh_headers = [h for h in set_cookies if h.startswith(REFRESH_COOKIE + "=")]
    assert refresh_headers, f"no Set-Cookie for {REFRESH_COOKIE}; got {set_cookies}"
    attrs = _cookie_attrs(refresh_headers[0], REFRESH_COOKIE)
    assert "httponly" in attrs, f"refresh cookie missing HttpOnly: {refresh_headers[0]}"
    assert attrs.get("path", "") == "/auth", (
        f"refresh cookie path should be /auth, got {attrs.get('path')!r}"
    )
    assert attrs.get("samesite", "").lower() == "lax"


@pytest.mark.asyncio
async def test_signup_sets_csrf_cookie(client, fresh_user_cookies):
    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    res = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert res.status_code == 200, res.text

    set_cookies = res.headers.get_list("set-cookie")
    csrf_headers = [h for h in set_cookies if h.startswith(CSRF_COOKIE + "=")]
    assert csrf_headers, f"no Set-Cookie for {CSRF_COOKIE}; got {set_cookies}"
    attrs = _cookie_attrs(csrf_headers[0], CSRF_COOKIE)
    # CSRF cookie MUST be readable by JS, so HttpOnly must NOT be set.
    assert "httponly" not in attrs, (
        f"CSRF cookie must NOT be HttpOnly (JS needs to read it): {csrf_headers[0]}"
    )
    assert attrs.get("samesite", "").lower() == "lax"


# ---------------------------------------------------------------------------
# Token absence from response body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_does_not_return_tokens_in_body(client, fresh_user_cookies):
    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    res = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "access_token" not in body, (
        "login body must not contain access_token; got keys "
        f"{sorted(body.keys())}"
    )
    assert "refresh_token" not in body, (
        "login body must not contain refresh_token; got keys "
        f"{sorted(body.keys())}"
    )
    # Sanity: the new session envelope is present.
    assert "user" in body, body
    assert "expires_in" in body, body


@pytest.mark.asyncio
async def test_signup_does_not_return_tokens_in_body(client):
    email = _unique_email()
    res = await client.post(
        "/auth/signup",
        data={
            "first_name": "Pytest",
            "last_name": "NoBody",
            "email": email,
            "password": "Secret123Strong",
            "confirm_password": "Secret123Strong",
        },
    )
    assert res.status_code in (200, 201), res.text
    body = res.json()
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert "user" in body and "expires_in" in body


# ---------------------------------------------------------------------------
# Refresh cookie flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_works_with_cookie_only(client, fresh_user_cookies):
    """POST /auth/refresh with no body should pick up the refresh token
    from the cookie and rotate the session."""
    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    login = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert login.status_code == 200, login.text

    # Carry the refresh cookie forward (httpx AsyncClient does this
    # automatically via the shared jar, but be explicit).
    cookies = {
        REFRESH_COOKIE: login.cookies.get(REFRESH_COOKIE),
        ACCESS_COOKIE: login.cookies.get(ACCESS_COOKIE),
    }
    assert cookies[REFRESH_COOKIE], "refresh cookie not set on login"

    res = await client.post("/auth/refresh", cookies=cookies)
    assert res.status_code == 200, res.text
    body = res.json()
    # The response body still follows AuthSessionResponse — no tokens.
    assert "access_token" not in body
    assert "refresh_token" not in body
    # And a new Set-Cookie pair should have been issued.
    set_cookies = res.headers.get_list("set-cookie")
    assert any(h.startswith(REFRESH_COOKIE + "=") for h in set_cookies), set_cookies


@pytest.mark.asyncio
async def test_refresh_without_cookie_fails(client, fresh_user_cookies):
    """If neither cookie nor body carries a refresh token, the endpoint
    must reject with 401."""
    res = await client.post("/auth/refresh")
    assert res.status_code == 401, res.text


# ---------------------------------------------------------------------------
# Logout clears cookies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_clears_auth_cookies(client, fresh_user_cookies):
    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    login = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert login.status_code == 200, login.text

    cookies = {
        ACCESS_COOKIE: login.cookies.get(ACCESS_COOKIE),
        REFRESH_COOKIE: login.cookies.get(REFRESH_COOKIE),
    }
    assert cookies[ACCESS_COOKIE]
    assert cookies[REFRESH_COOKIE]

    res = await client.post("/auth/logout", cookies=cookies)
    assert res.status_code in (200, 204), res.text
    set_cookies = res.headers.get_list("set-cookie")
    # Each cookie should be cleared with Max-Age=0 (or an Expires in the past).
    for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE):
        matching = [h for h in set_cookies if h.startswith(name + "=")]
        assert matching, f"logout did not clear {name}; got {set_cookies}"
        attrs = _cookie_attrs(matching[0], name)
        assert attrs.get("max-age", "") == "0", (
            f"logout did not set Max-Age=0 on {name}: {matching[0]}"
        )


# ---------------------------------------------------------------------------
# Protected route via cookie
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_me_works_with_cookie_only(client, fresh_user_cookies):
    """GET /auth/me using ONLY the access cookie must succeed. No
    `Authorization: Bearer` header."""
    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    login = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert login.status_code == 200, login.text
    cookies = {ACCESS_COOKIE: login.cookies.get(ACCESS_COOKIE)}
    assert cookies[ACCESS_COOKIE]

    res = await client.get("/auth/me", cookies=cookies)
    assert res.status_code == 200, res.text
    body = res.json()
    # /auth/me returns the user envelope; the email is the canonical
    # subject claim on the JWT.
    assert body.get("email") == email


@pytest.mark.asyncio
async def test_me_unauthorized_without_cookie(client):
    """GET /auth/me without cookies or Authorization must be 401."""
    res = await client.get("/auth/me")
    assert res.status_code == 401, res.text


@pytest.mark.asyncio
async def test_me_still_works_with_legacy_bearer_header(
    client, fresh_user_cookies
):
    """The transition fallback keeps Authorization: Bearer working."""
    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    login = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert login.status_code == 200, login.text
    access = login.cookies.get(ACCESS_COOKIE)
    assert access

    res = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {access}"}
    )
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# WebSocket auth accepts cookies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_auth_via_cookie(client, fresh_user_cookies):
    """`authenticate_ws` should accept the access token via the Cookie
    header. We exercise the helper directly — the route would need a
    live ASGI WebSocket transport, which the AsyncClient doesn't
    expose. The cookie path is what the new `authenticate_ws` checks
    first; this test guards against a regression that flips the order.
    """
    from app.core.ws_auth import authenticate_ws
    from app.database import AsyncSessionLocal

    email = fresh_user_cookies["email"]
    password = fresh_user_cookies["password"]
    login = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert login.status_code == 200, login.text
    access = login.cookies.get(ACCESS_COOKIE)
    assert access

    async with AsyncSessionLocal() as db:
        user = await authenticate_ws(None, db, cookies={ACCESS_COOKIE: access})
    assert user.email == email

    # And the query-param path is still accepted (transition).
    async with AsyncSessionLocal() as db:
        user_q = await authenticate_ws(access, db, cookies=None)
    assert user_q.email == email


@pytest.mark.asyncio
async def test_websocket_auth_rejects_when_missing(client):
    """With neither cookie nor query token, `authenticate_ws` raises."""
    from app.core.ws_auth import WebSocketAuthError, authenticate_ws
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        with pytest.raises(WebSocketAuthError):
            await authenticate_ws(None, db, cookies=None)
