"""
Seed realistic test data for the bench suite.

`seed_baseline(n_users, n_posts, comments_per_post, replies_per_comment,
votes_per_post)` inserts a connected graph:

  n_users users
    n_posts posts, one per user (cycle)
      comments_per_post top-level comments
        replies_per_comment reply on each
      votes_per_post votes from random users

The numbers chosen in the bench scripts roughly mirror what a real
deploy sees on the timeline endpoint: ~20 posts, ~5 comments each,
~2 replies per comment, ~10 likes per post.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


async def seed_baseline(
    db: AsyncSession,
    *,
    n_users: int = 25,
    n_posts: int = 20,
    comments_per_post: int = 5,
    replies_per_comment: int = 2,
    votes_per_post: int = 10,
) -> dict:
    """Seed the in-memory DB and return a dict of reference ids."""

    # Roles needed by the routers: at least one of each.
    users = []
    now = datetime.utcnow()
    for i in range(n_users):
        role = (
            models.Role.ADMIN if i == 0
            else models.Role.JOURNALIST if i == 1
            else models.Role.MP if i == 2
            else models.Role.CITIZEN
        )
        u = models.User(
            first_name=f"User{i:03d}",
            last_name=f"Last{i:03d}",
            username=f"user{i:03d}",
            email=f"user{i:03d}@example.com",
            hashed_password="x",
            is_active=True,
            role=role,
            region="Central",
            district_id=f"dist_{i % 5}",
            created_at=now - timedelta(days=30),
        )
        db.add(u)
        users.append(u)
    await db.flush()

    # Posts
    posts = []
    for i in range(n_posts):
        author = users[i % len(users)]
        p = models.Post(
            title=f"Post {i:03d}",
            content=f"Body of post {i:03d}. " * 10,
            author_id=author.id,
            district_id=f"dist_{i % 5}",
            group_id=None,
            created_at=now - timedelta(hours=i),
            share_count=0,
        )
        db.add(p)
        posts.append(p)
    await db.flush()

    # Votes — 10 per post from random users (avoid the author)
    vote_rows = []
    for p in posts:
        for j in range(votes_per_post):
            voter = users[(p.id + j) % len(users)]
            if voter.id == p.author_id:
                continue
            vote_rows.append({
                "user_id": voter.id,
                "post_id": p.id,
                "vote_type": "like",
                "created_at": now,
            })
    if vote_rows:
        await db.execute(insert(models.Vote), vote_rows)

    # Comments + replies
    comment_rows = []
    reply_rows = []
    for p in posts:
        for c in range(comments_per_post):
            author = users[(p.id + c) % len(users)]
            comment_rows.append({
                "content": f"Comment {c} on post {p.id}.",
                "author_id": author.id,
                "post_id": p.id,
                "parent_id": None,
                "created_at": now - timedelta(minutes=10 * c),
                "updated_at": now - timedelta(minutes=10 * c),
            })
    if comment_rows:
        await db.execute(insert(models.Comment), comment_rows)
    await db.flush()

    # Replies — fetch top-level comments for parent_ids.
    from sqlalchemy import select
    res = await db.execute(select(models.Comment).where(models.Comment.parent_id.is_(None)))
    top_level = res.scalars().all()
    for c in top_level:
        for r in range(replies_per_comment):
            author = users[(c.id + r) % len(users)]
            reply_rows.append({
                "content": f"Reply {r} to comment {c.id}.",
                "author_id": author.id,
                "post_id": c.post_id,
                "parent_id": c.id,
                "created_at": now,
                "updated_at": now,
            })
    if reply_rows:
        await db.execute(insert(models.Comment), reply_rows)

    # Live feed + messages (one of each)
    feed = models.LiveFeed(
        post_id=posts[0].id if posts else None,
        journalist_id=users[1].id,
        content="Breaking news",
        district_id="dist_0",
        is_active=True,
        created_at=now,
    )
    db.add(feed)
    await db.flush()

    for k in range(5):
        db.add(models.LiveFeedMessage(
            feed_id=feed.id,
            user_id=users[k % len(users)].id,
            message=f"Live message {k}",
            created_at=now,
        ))

    # Notifications
    for k in range(10):
        db.add(models.Notification(
            user_id=users[0].id,
            type=models.NotificationType.SYSTEM,
            message=f"Notification {k}",
            is_read=False,
            created_at=now - timedelta(minutes=k),
        ))

    # Group + members
    g = models.Group(
        name=f"group_{now.timestamp()}",
        description="A test group",
        owner_id=users[0].id,
        is_active=True,
        created_at=now,
    )
    db.add(g)
    await db.flush()

    for u in users[:8]:
        await db.execute(insert(models.group_members).values(group_id=g.id, user_id=u.id))

    await db.commit()

    return {
        "users": [u.id for u in users],
        "posts": [p.id for p in posts],
        "live_feed": feed.id,
        "group": g.id,
        "admin": users[0].id,
    }
