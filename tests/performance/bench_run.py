"""
Benchmark runner for the hot list endpoints.

Run with:

    cd backend/CIVCON
    venv/Scripts/python.exe -m tests.performance.bench_run

This script:
  1. Spins up an in-memory SQLite engine.
  2. Replaces the app's `get_db` with a SessionLocal bound to that
     engine (the engine and AsyncSessionLocal module-level refs in
     `app.database` are swapped, so every router that imported them
     gets the new one too).
  3. Patches the FastAPI app's `dependency_overrides` for the auth
     dependencies (`oauth2.get_current_user`, every `require_role`
     closure registered on a route, and `oauth2_scheme`) so the
     handlers see a fake logged-in user.
  4. Seeds a realistic dataset (20 users, 20 posts, 5 comments/post,
     2 replies/comment, 10 votes/post, plus a live feed and a group).
  5. Calls each benchmarked endpoint via `TestClient` and records
     p50/p95 latency + SQL-statement count over N iterations.
  6. Writes a markdown table to `bench_reports/measured.md`.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make `app` importable when this file is run directly.
THIS_DIR = Path(__file__).resolve().parent
BACKEND = THIS_DIR.parent.parent
sys.path.insert(0, str(BACKEND))

# Provide the same minimum env vars the Settings class expects.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("DATABASE_HOSTNAME", "localhost")
os.environ.setdefault("DATABASE_PORT", "5432")
os.environ.setdefault("DATABASE_NAME", "civcon_test")
os.environ.setdefault("DATABASE_USERNAME", "test")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-prod")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "60")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test")
os.environ.setdefault("LINKEDIN_CLIENT_ID", "test")
os.environ.setdefault("LINKEDIN_CLIENT_SECRET", "test")
os.environ.setdefault("AFRICASTALKING_USERNAME", "test")
os.environ.setdefault("AFRICASTALKING_API_KEY", "test")
os.environ.setdefault("MAIL_USERNAME", "test")
os.environ.setdefault("MAIL_PASSWORD", "test")
os.environ.setdefault("MAIL_FROM", "test@example.com")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("FRONTEND_URL", "http://localhost:5173")
os.environ.setdefault("BACKEND_URL", "http://localhost:8000")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test")
os.environ.setdefault("CLOUDINARY_API_KEY", "test")
os.environ.setdefault("CLOUDINARY_API_SECRET", "test")
os.environ.setdefault("RESEND_API_KEY", "test")
os.environ.setdefault("SENDER_EMAIL", "test@example.com")

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Stub out modules that need real secrets / network and that
# aren't relevant to the bench. Several routers have pre-existing
# case-mismatched access to pydantic Settings attributes (e.g.
# `settings.AFRICASTALKING_USERNAME` while the field is
# `africastalking_username`); pydantic-settings is case-insensitive
# at the env-var level but the Python attribute lookup IS
# case-sensitive. Those imports crash when the env var is set.
# We replace the offending routers with no-op modules before the
# app's `from app.routers import (...)` runs.
import types as _types
import sys as _sys


def _make_stub(name: str, attrs: dict | None = None) -> _types.ModuleType:
    m = _types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(m, k, v)
    return m


# Pre-stub dependencies of the broken routers.
_sys.modules["app.utils.email_utils"] = _make_stub(
    "app.utils.email_utils",
    {
        "send_reset_email": lambda *a, **kw: None,
        "send_email": lambda *a, **kw: None,
        "send_email_background": lambda *a, **kw: None,
        "send_verification_email": lambda *a, **kw: None,
        "RESEND_API_KEY": "test",
        "SENDER_EMAIL": "test@example.com",
    },
)
_sys.modules["app.spam_detector"] = _make_stub(
    "app.spam_detector",
    {"download_nltk_resources": lambda: None},
)
# Stub the `africastalking` SDK so the broken routers don't crash.
_sys.modules["africastalking"] = _make_stub(
    "africastalking",
    {
        "initialize": lambda *a, **kw: None,
        "SMS": _make_stub("africastalking.SMS", {"send": lambda *a, **kw: None}),
        "Voice": _make_stub("africastalking.Voice", {}),
        "Payment": _make_stub("africastalking.Payment", {}),
    },
)
# Stub the broken routers themselves so app.main can import them.
# We expose an empty `APIRouter` for each — the bench never hits
# these routes.
from fastapi import APIRouter as _APIRouter
for _rname in (
    "app.routers.mp",
    "app.routers.ussd",
):
    _sys.modules[_rname] = _make_stub(_rname, {"router": _APIRouter()})

from app import database as app_db
from app import models  # noqa: F401
from app.database import Base
from app.main import app
from app.routers import oauth2
from app.routers.permissions import require_admin, require_role

from .bench_common import (
    QueryCounter,
    render_markdown_table,
    run_bench,
)
from .seed_data import seed_baseline


# ---------------------------------------------------------------------------
# Build a fresh in-memory engine and seed it
# ---------------------------------------------------------------------------


async def setup_app() -> tuple:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        pool_pre_ping=False,
    )
    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    app_db.engine = engine
    app_db.AsyncSessionLocal = SessionLocal

    async def _get_db_override():
        async with SessionLocal() as session:
            yield session

    app_db.get_db = _get_db_override
    app.dependency_overrides[app_db.get_db] = _get_db_override

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as db:
        ids = await seed_baseline(db)

    return engine, SessionLocal, ids


# ---------------------------------------------------------------------------
# Auth override — make every auth dep return a fake user
# ---------------------------------------------------------------------------


def install_auth_override(user_id: int, role_value: str):
    """Make every `get_current_user` / `require_role` call return `user_id`."""
    fake_user = models.User(
        id=user_id,
        first_name="Bench",
        last_name="User",
        username="bench",
        email="bench@example.com",
        hashed_password="x",
        is_active=True,
        role=models.Role(role_value),
        region="Central",
        district_id="dist_0",
    )

    async def _fake_current_user():
        return fake_user

    app.dependency_overrides[oauth2.get_current_user] = _fake_current_user
    app.dependency_overrides[require_admin] = _fake_current_user
    # Also override any `require_role([...])` closure that has been
    # registered as a route dependency. We do that by walking the
    # app's routes and overriding the dependant.call attribute when
    # it isn't already overridden.
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependant.dependencies:
            call = dep.call
            if call in app.dependency_overrides:
                continue
            # `require_role` returns a closure named `dependency`.
            # Heuristic: any dep whose name is "dependency" and whose
            # closure vars include `get_current_user` is one.
            if getattr(call, "__name__", "") == "dependency":
                app.dependency_overrides[call] = _fake_current_user


# ---------------------------------------------------------------------------
# Build a bench-only FastAPI app (no middleware noise)
# ---------------------------------------------------------------------------


def build_bench_app() -> FastAPI:
    """Return a sibling FastAPI app that uses the same routes but
    skips the production middleware stack (RequestId, AccessLog,
    Prometheus, RequestSizeLimit, RequestTimeout, CORS, Session).
    The dependency overrides we installed on the main app are not
    visible to this sibling — install them on this app too.
    """
    bench = FastAPI()
    for route in app.routes:
        if isinstance(route, APIRoute):
            bench.router.routes.append(route)
    bench.dependency_overrides.update(app.dependency_overrides)
    # Also rebind the get_db override on the sibling.
    return bench


# ---------------------------------------------------------------------------
# The benchmarked endpoints
# ---------------------------------------------------------------------------


def call(client: TestClient, method: str, path: str):
    return client.request(method, path)


async def bench_all(iterations: int = 50) -> list:
    engine, SessionLocal, ids = await setup_app()
    install_auth_override(ids["admin"], "admin")

    bench_app = build_bench_app()
    client = TestClient(bench_app, raise_server_exceptions=False)

    results = []

    # --- 1. GET /posts/ (timeline) ---
    async def call_list_posts():
        call(client, "GET", "/posts/")

    results.append(await run_bench(
        "GET /posts/ (timeline, limit=20)",
        iterations,
        call_list_posts,
        notes="20 posts × (author + media + votes + comments→author + comments→replies→author)",
    ))

    # --- 2. GET /posts/{id} ---
    post_id = ids["posts"][0]
    async def call_get_post():
        call(client, "GET", f"/posts/{post_id}")

    results.append(await run_bench(
        "GET /posts/{id}",
        iterations,
        call_get_post,
        notes="Single post + comments (recursive)",
    ))

    # --- 3. GET /posts/{id}/comments ---
    async def call_get_comments():
        call(client, "GET", f"/posts/{post_id}/comments")

    results.append(await run_bench(
        "GET /posts/{id}/comments",
        iterations,
        call_get_comments,
        notes="Top-level comments + replies (2 levels)",
    ))

    # --- 4. GET /live-feeds/ ---
    async def call_list_live_feeds():
        call(client, "GET", "/live-feeds/")

    results.append(await run_bench(
        "GET /live-feeds/",
        iterations,
        call_list_live_feeds,
        notes="Live feeds (no auth required)",
    ))

    # --- 5. GET /live-feeds/{id}/messages ---
    feed_id = ids["live_feed"]
    async def call_list_live_feed_msgs():
        call(client, "GET", f"/live-feeds/{feed_id}/messages")

    results.append(await run_bench(
        "GET /live-feeds/{id}/messages",
        iterations,
        call_list_live_feed_msgs,
        notes="Live feed messages (already eager-loaded)",
    ))

    # --- 6. GET /notifications/ ---
    async def call_list_notifications():
        call(client, "GET", "/notifications/")

    results.append(await run_bench(
        "GET /notifications/",
        iterations,
        call_list_notifications,
        notes="Current user's notifications (auth required)",
    ))

    # --- 7. GET /groups/{id}/posts ---
    group_id = ids["group"]
    async def call_get_group_posts():
        call(client, "GET", f"/groups/{group_id}/posts")

    results.append(await run_bench(
        "GET /groups/{id}/posts",
        iterations,
        call_get_group_posts,
        notes="Posts in a group + like/comment counts (2N+ queries)",
    ))

    # --- 8. GET /groups/ ---
    async def call_list_groups():
        call(client, "GET", "/groups/")

    results.append(await run_bench(
        "GET /groups/",
        iterations,
        call_list_groups,
        notes="All groups (no auth)",
    ))

    # --- 9. GET /users/by-username/{username} ---
    async def call_get_user_by_username():
        call(client, "GET", "/users/by-username/user000")

    results.append(await run_bench(
        "GET /users/by-username/{u}",
        iterations,
        call_get_user_by_username,
        notes="Public profile + followers count (currently sequential)",
    ))

    return results


def write_report(results, out_path: Path) -> None:
    body = [
        "# Backend performance — measured numbers",
        "",
        "_Generated by `tests/performance/bench_run.py`._",
        "",
        "All numbers are from a 20-user, 20-post, 5-comments-per-post,",
        "2-replies-per-comment, 10-votes-per-post SQLite dataset.",
        "",
        "Each benchmark is the median of N runs over the same hot path.",
        "Wall-clock is in milliseconds; `q p50` is the median number of",
        "SQL statements issued during one request.",
        "",
        render_markdown_table(results, "Results"),
    ]
    out_path.write_text("\n".join(body), encoding="utf-8")
    print(f"Wrote {out_path}")


def main() -> None:
    iterations = int(os.environ.get("BENCH_ITERATIONS", "50"))
    results = asyncio.run(bench_all(iterations=iterations))
    out = BACKEND / "bench_reports" / "measured.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(results, out)
    print()
    for r in results:
        print(
            f"{r.name:48s}  p50={r.p50_ms:6.2f}ms  "
            f"p95={r.p95_ms:7.2f}ms  q p50={r.queries_p50:3d}"
        )


if __name__ == "__main__":
    main()
