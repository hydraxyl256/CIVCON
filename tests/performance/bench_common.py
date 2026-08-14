"""
Shared infrastructure for performance benchmarks.

The benchmarks import a real FastAPI app but substitute its Postgres
engine with a fresh in-memory SQLite engine. This is fine for N+1
detection and latency profiling — what changes between "before" and
"after" is the shape of the SQL each router issues, which both
databases respect.

The benchmark harness:

  - Patches `app.database.engine` and `app.database.get_db` to point
    at a single shared SQLite engine per test.
  - Seeds N users/posts/comments/etc. via direct ORM calls (no HTTP).
  - Installs a low-level SQLAlchemy event listener that counts every
    statement issued during a timed call.
  - Times the call with `time.perf_counter` and reports p50/p95 over
    `iterations` runs.

The router endpoints are called directly (bypassing FastAPI's HTTP
layer) so we skip middleware overhead and measure pure handler cost.
"""
from __future__ import annotations

import contextlib
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

# ---------------------------------------------------------------------------
# Test environment
# ---------------------------------------------------------------------------
# Provide minimum required env vars BEFORE importing the app so that
# pydantic Settings validation succeeds.
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

# ---------------------------------------------------------------------------
# App imports (after env setup)
# ---------------------------------------------------------------------------

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import database as app_db
from app import models  # noqa: F401 - registers models on Base
from app.database import Base


# ---------------------------------------------------------------------------
# Engine + session override
# ---------------------------------------------------------------------------


def make_sqlite_engine():
    """Create a fresh in-memory async SQLite engine for one bench run."""
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        pool_pre_ping=False,
    )


def patch_app_engine(engine) -> async_sessionmaker:
    """
    Swap the app's global engine + get_db dependency to use the bench
    engine, and create all tables. Returns the session factory the
    benchmark should use to seed data.
    """
    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Override the module-level engine and AsyncSessionLocal.
    app_db.engine = engine
    app_db.AsyncSessionLocal = SessionLocal

    # Replace get_db so that routers/handlers that use it get the
    # bench session. The benchmarks can also pass an explicit session
    # — these two paths are equivalent.
    async def _get_db_override():
        async with SessionLocal() as session:
            yield session

    app_db.get_db = _get_db_override

    return SessionLocal


async def create_all(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ---------------------------------------------------------------------------
# Query counting
# ---------------------------------------------------------------------------


@dataclass
class QueryCounter:
    """A scope that counts SQL statements issued inside it."""

    count: int = 0
    _listener_handle: Any = None
    _engine: Any = None

    def attach(self, sync_engine) -> None:
        self._engine = sync_engine
        self.count = 0

        @event.listens_for(sync_engine, "before_cursor_execute")
        def _on_execute(conn, cursor, statement, parameters, context, executemany):
            self.count += 1

        self._listener_handle = _on_execute

    def detach(self) -> None:
        if self._listener_handle and self._engine:
            with contextlib.suppress(Exception):
                event.remove(self._listener_handle, self._engine, "before_cursor_execute")
        self._listener_handle = None
        self._engine = None


# ---------------------------------------------------------------------------
# Timing + reporting
# ---------------------------------------------------------------------------


@dataclass
class BenchResult:
    name: str
    iterations: int
    p50_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    mean_ms: float
    queries_min: int
    queries_p50: int
    queries_max: int
    notes: str = ""

    def to_markdown_row(self) -> str:
        return (
            f"| {self.name} | {self.iterations} | "
            f"{self.p50_ms:.2f} | {self.p95_ms:.2f} | "
            f"{self.queries_min} | {self.queries_p50} | {self.queries_max} |"
        )


def percentiles(values: List[float], ps: List[float]) -> List[float]:
    if not values:
        return [0.0 for _ in ps]
    s = sorted(values)
    out = []
    for p in ps:
        if p <= 0:
            out.append(s[0])
        elif p >= 100:
            out.append(s[-1])
        else:
            idx = (len(s) - 1) * (p / 100.0)
            lo = int(idx)
            hi = min(lo + 1, len(s) - 1)
            frac = idx - lo
            out.append(s[lo] + (s[hi] - s[lo]) * frac)
    return out


async def run_bench(
    name: str,
    iterations: int,
    call: Callable[[], Any],
    *,
    notes: str = "",
) -> BenchResult:
    """
    Run `call()` `iterations` times. Return a BenchResult with timing
    and query-count percentiles.

    `call` must be an async callable that takes no args. It should use
    the same SessionLocal we already patched into the app — that way
    every statement it issues gets counted by the QueryCounter.
    """
    times: List[float] = []
    counts: List[int] = []

    # Get the sync engine underlying the async engine. SQLAlchemy's
    # async engine exposes the sync engine via .sync_engine.
    sync_eng = app_db.engine.sync_engine

    for _ in range(iterations):
        counter = QueryCounter()
        counter.attach(sync_eng)
        t0 = time.perf_counter()
        try:
            await call()
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            times.append(elapsed_ms)
            counts.append(counter.count)
            counter.detach()

    p50, p95 = percentiles(times, [50, 95])
    q_min, q_p50, q_max = percentiles(counts, [0, 50, 100])

    return BenchResult(
        name=name,
        iterations=iterations,
        p50_ms=p50,
        p95_ms=p95,
        min_ms=min(times),
        max_ms=max(times),
        mean_ms=statistics.fmean(times),
        queries_min=int(q_min),
        queries_p50=int(q_p50),
        queries_max=int(q_max),
        notes=notes,
    )


def render_markdown_table(results: List[BenchResult], title: str) -> str:
    lines = [
        f"## {title}",
        "",
        "| Benchmark | Iters | p50 (ms) | p95 (ms) | q min | q p50 | q max |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(r.to_markdown_row())
    lines.append("")
    return "\n".join(lines)
