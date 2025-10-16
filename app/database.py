from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
import ssl
from .config import settings

DATABASE_URL = settings.database_url

# Detect if running on Render (or any hosted DB)
# Render Postgres requires SSL; local typically does not.
connect_args = {}

if "render.com" in DATABASE_URL or os.getenv("RENDER") == "true":
    # Enforce SSL for hosted DBs
    connect_args = {"ssl": {"sslmode": "require"}}

# Create async engine with SSL support
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args
)

# Async session factory
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# Base class for models
Base = declarative_base()

# Dependency
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
