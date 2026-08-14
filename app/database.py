import logging

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

logger = logging.getLogger("CIVCON.database")

# Use the DATABASE_URL directly (e.g. includes ?sslmode=require for Postgres).
# asyncpg reads the sslmode from the URL; do NOT pass a custom ssl context —
# doing so disables certificate verification (CVE-prone).
DATABASE_URL = settings.database_url

# If a custom connect_args dict is needed in the future, derive it from the
# platform (Render / RDS / Cloud SQL) and never call ssl._create_unverified_context().
connect_args: dict = {}

# Async engine
#
# Pool sizing is driven by env vars (db_pool_size, db_max_overflow,
# db_pool_recycle, db_pool_timeout) so each environment can tune the
# connection budget without a code change. Defaults come from Settings.
#
# pool_recycle=1800s keeps connections under the typical 30-min idle
# timeout enforced by cloud load balancers (Render, Railway, AWS ALB).
# pool_pre_ping=True adds a cheap round-trip on checkout to detect
# connections that the server has silently closed.
# pool_use_lifo=True makes the pool hand out the *most-recently used*
# connection first (instead of FIFO). On busy servers this warms the
# connection state in cache and avoids opening new TCP/TLS sessions
# more often than necessary. SQLAlchemy documents this as a stable
# latency-friendly option since 2.0; no application-level change is
# required.
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,
    pool_timeout=settings.db_pool_timeout,
    pool_pre_ping=True,
    pool_use_lifo=True,
)

# Async session factory
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


# Dependency
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
