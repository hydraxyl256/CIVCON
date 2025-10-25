import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.future import select
from app.models import User
from app.config import settings


#  Load DATABASE_URL safely (from settings or .env)
DATABASE_URL = getattr(settings, "database_url", None) or os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError(" DATABASE_URL not found in settings or environment.")

#  Ensure Render DB URL includes port (Render usually omits it sometimes)
if "@" in DATABASE_URL and ":5432/" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace(".render.com/", ".render.com:5432/")

print(f"Connecting to: {DATABASE_URL}")

#  Create async engine and session
engine = create_async_engine(DATABASE_URL, echo=True, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def backfill_usernames():
    print(" Starting username backfill...")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        users = result.scalars().all()

        updated = 0
        for u in users:
            if not u.username:
                base_username = f"{u.first_name.lower()}{u.last_name.lower()}_{u.id}"
                u.username = base_username
                db.add(u)
                updated += 1

        await db.commit()
        print(f" Successfully backfilled {updated} usernames.")


if __name__ == "__main__":
    asyncio.run(backfill_usernames())
