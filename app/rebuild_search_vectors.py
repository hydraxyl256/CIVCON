import asyncio

from sqlalchemy import text

from app.database import AsyncSessionLocal as async_session_maker


async def rebuild_vectors():
    async with async_session_maker() as db:
        print("🔄 Rebuilding search vectors...")

        # Users
        await db.execute(text("""
            UPDATE users
            SET search_vector = to_tsvector('english',
                coalesce(username, '') || ' ' ||
                coalesce(first_name, '') || ' ' ||
                coalesce(last_name, '') || ' ' ||
                coalesce(bio, '')
            );
        """))
        print("✅ Users updated")

        # Posts
        await db.execute(text("""
            UPDATE posts
            SET search_vector = to_tsvector('english',
                coalesce(title, '') || ' ' || coalesce(content, '')
            );
        """))
        print("✅ Posts updated")

        # Comments
        await db.execute(text("""
            UPDATE comments
            SET search_vector = to_tsvector('english',
                coalesce(content, '')
            );
        """))
        print("✅ Comments updated")

        # Articles
        await db.execute(text("""
            UPDATE articles
            SET search_vector = to_tsvector('english',
                coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(content, '')
            );
        """))
        print("✅ Articles updated")

        await db.commit()
        print("🎉 All search vectors rebuilt successfully!")


if __name__ == "__main__":
    asyncio.run(rebuild_vectors())
