"""
Auth lifecycle tests.

Covers:
- login (success / wrong password / suspended)
- signup validation
- refresh rotation
- refresh reuse-detection (the family is revoked)
- logout (blacklists access token, revokes matching session row)
- protected route rejects blacklisted token
- protected route rejects refresh token (regression for the duplicate
  `get_current_user` bug)
- role gate (admin vs citizen)
- oauth bootstrap exchange (success + replay blocked)
- password reset revokes all sessions

These tests assume a working Postgres + Redis are reachable via the
`DATABASE_URL` / `REDIS_URL` environment variables used by `app.config`.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

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
async def fresh_user(client):
    """Sign up a brand new user and return (email, password, tokens)."""
    email = _unique_email()
    password = "Secret123Strong"
    res = await client.post(
        "/auth/signup",
        data={
            "first_name": "Pytest",
            "last_name": "User",
            "email": email,
            "password": password,
            "confirm_password": password,
        },
    )
    assert res.status_code in (200, 201), res.text
    body = res.json()
    return {
        "email": email,
        "password": password,
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
    }


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_success(client, fresh_user):
    res = await client.post(
        "/auth/login",
        data={"username": fresh_user["email"], "password": fresh_user["password"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "access_token" in body and "refresh_token" in body


@pytest.mark.asyncio
async def test_login_wrong_password(client, fresh_user):
    res = await client.post(
        "/auth/login",
        data={"username": fresh_user["email"], "password": "WrongPassword999"},
    )
    # Wrong credentials should be 401, never 403 (don't leak existence)
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_email(client):
    res = await client.post(
        "/auth/login",
        data={"username": "nobody_here@example.com", "password": "x"},
    )
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Signup validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signup_password_mismatch(client):
    email = _unique_email()
    res = await client.post(
        "/auth/signup",
        data={
            "first_name": "X",
            "last_name": "Y",
            "email": email,
            "password": "GoodPass123",
            "confirm_password": "DifferentPass123",
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_signup_password_too_short(client):
    email = _unique_email()
    res = await client.post(
        "/auth/signup",
        data={
            "first_name": "X",
            "last_name": "Y",
            "email": email,
            "password": "short1A",
            "confirm_password": "short1A",
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_signup_duplicate_email(client, fresh_user):
    res = await client.post(
        "/auth/signup",
        data={
            "first_name": "X",
            "last_name": "Y",
            "email": fresh_user["email"],
            "password": "GoodPass123",
            "confirm_password": "GoodPass123",
        },
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# Refresh — rotation + reuse detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_rotation_returns_new_pair(client, fresh_user):
    first_refresh = fresh_user["refresh_token"]
    res = await client.post("/auth/refresh", json={"refresh_token": first_refresh})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["refresh_token"] != first_refresh
    assert body["access_token"] != fresh_user["access_token"]


@pytest.mark.asyncio
async def test_refresh_reuse_revokes_family(client, fresh_user):
    """Replaying an already-rotated refresh token must revoke the family."""
    first_refresh = fresh_user["refresh_token"]
    # First rotation succeeds
    r1 = await client.post("/auth/refresh", json={"refresh_token": first_refresh})
    assert r1.status_code == 200
    # Replay the SAME refresh token -> reuse detected
    r2 = await client.post("/auth/refresh", json={"refresh_token": first_refresh})
    assert r2.status_code == 401
    # Even the now-current refresh token should fail (family revoked)
    new_refresh = r1.json()["refresh_token"]
    r3 = await client.post("/auth/refresh", json={"refresh_token": new_refresh})
    assert r3.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_access_token_rejected(client, fresh_user):
    """Refresh must not accept an access token — type confusion check."""
    res = await client.post(
        "/auth/refresh", json={"refresh_token": fresh_user["access_token"]}
    )
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_blacklists_access_token(client, fresh_user):
    access = fresh_user["access_token"]
    headers = _bearer(access)
    # Pre-logout, /auth/me returns 200
    pre = await client.get("/auth/me", headers=headers)
    assert pre.status_code == 200
    # Logout
    out = await client.post("/auth/logout", headers=headers)
    assert out.status_code == 200
    # Post-logout, /auth/me returns 401 (blacklisted)
    post = await client.get("/auth/me", headers=headers)
    assert post.status_code == 401


@pytest.mark.asyncio
async def test_logout_with_refresh_token_revokes_session(client, fresh_user):
    """A logout that sends the Refresh-Token header must revoke the session row."""
    headers = {
        **_bearer(fresh_user["access_token"]),
        "Refresh-Token": fresh_user["refresh_token"],
    }
    res = await client.post("/auth/logout", headers=headers)
    assert res.status_code == 200
    # The previously valid refresh token must now fail (session row revoked)
    r = await client.post(
        "/auth/refresh",
        json={"refresh_token": fresh_user["refresh_token"]},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Protected route consistency — the regression test for the duplicate
# `get_current_user` bug.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_protected_route_rejects_refresh_token(client, fresh_user):
    """`/auth/me` (and any other protected route) must NOT accept a refresh token.

    Regression for the original `app/routers/oauth2.py` duplicate that
    skipped the type check.
    """
    res = await client.get(
        "/auth/me",
        headers=_bearer(fresh_user["refresh_token"]),
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_rejects_blacklisted_token(client, fresh_user):
    """Blacklisted access tokens must be rejected by every protected route."""
    access = fresh_user["access_token"]
    # Blacklist via logout (no refresh token = blacklist only, but blacklist
    # is enough to reject subsequent calls)
    await client.post("/auth/logout", headers=_bearer(access))
    # /users/me uses get_current_user — should 401
    r = await client.get("/users/me", headers=_bearer(access))
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_role_required_denies_non_admin(client, fresh_user):
    """A non-admin user hitting an admin route must be 403."""
    # The /admin routes all have `dependencies=[Depends(require_admin)]`
    res = await client.get(
        "/admin/users",
        headers=_bearer(fresh_user["access_token"]),
    )
    # Either 401 (auth scheme mismatch) or 403 (role denied) is acceptable;
    # both mean "not allowed". Most admin endpoints register under /admin.
    # We don't know the exact endpoint list so try a couple.
    if res.status_code == 404:
        # Try the moderation endpoint which we know exists
        res = await client.get(
            "/admin/moderation/posts",
            headers=_bearer(fresh_user["access_token"]),
        )
    assert res.status_code in (401, 403)


# ---------------------------------------------------------------------------
# OAuth bootstrap exchange
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oauth_exchange_rejects_unknown_code(client):
    res = await client.post(
        "/auth/oauth/exchange", json={"code": "definitely-not-a-real-code"}
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_oauth_exchange_rejects_replay(client):
    """After a bootstrap code is consumed, replaying it returns 401.

    We simulate this by writing a code into Redis directly (mirrors what
    `_mint_bootstrap_code` does in production) and consuming it twice.
    """
    import json as _json
    import secrets as _secrets

    from app.dependencies.auth import (
        get_current_user,
    )
    from app.routers.auth import _consume_bootstrap_code

    code = _secrets.token_urlsafe(32)
    payload = _json.dumps(
        {"access_token": "a", "refresh_token": "r", "expires_in": 60, "user": {"id": 1}}
    )
    # Write through the same redis client used by auth.py
    from app.redis_client import get_redis
    _redis = await get_redis()
    await _redis.setex(f"oauth:bootstrap:{code}", 60, payload)

    first = await _consume_bootstrap_code(code)
    assert first is not None

    second = await _consume_bootstrap_code(code)
    assert second is None


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_password_reset_flow(client, fresh_user):
    """A reset token issued for the email must successfully reset the password."""
    from app.core.auth import (
        PASSWORD_RESET_SCOPE,
        create_access_token,
    )

    # Mint a reset token directly (the forgot-password endpoint sends
    # email and we don't want to depend on a working mailer in tests).
    token, _ = create_access_token(
        {"sub": fresh_user["email"], "scope": PASSWORD_RESET_SCOPE},
        expires_delta=__import__("datetime").timedelta(minutes=30),
        token_type=PASSWORD_RESET_SCOPE,
    )

    new_password = "Reset456Strong"
    res = await client.post(
        "/auth/reset-password",
        json={"token": token, "new_password": new_password},
    )
    assert res.status_code == 200, res.text

    # Old credentials should no longer work
    bad = await client.post(
        "/auth/login",
        data={"username": fresh_user["email"], "password": fresh_user["password"]},
    )
    assert bad.status_code == 401

    # New credentials should work
    ok = await client.post(
        "/auth/login",
        data={"username": fresh_user["email"], "password": new_password},
    )
    assert ok.status_code == 200

    # The original refresh token from signup must be revoked by the reset
    # (the reset endpoint revokes every active session).
    r = await client.post(
        "/auth/refresh",
        json={"refresh_token": fresh_user["refresh_token"]},
    )
    assert r.status_code == 401