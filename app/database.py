from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import ssl
from .config import settings

DATABASE_URL = settings.database_url

# Create SSL context that skips certificate verification 
ssl_context = ssl._create_unverified_context()
connect_args = {"ssl": ssl_context}

# Async engine
engine = create_async_engine(DATABASE_URL, echo=True, connect_args=connect_args)

# Async session factory
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()

# Dependency
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
