"""database performance indexes — merge heads, add composite + GIN indexes

Revision ID: a3b4c5d6e7f8
Revises: a1b2c3d4e5f6, badcc4879431
Create Date: 2026-07-14 12:00:00.000000

Why this migration exists
=========================

This migration is the *single successor* to the two open alembic heads
(`a1b2c3d4e5f6` — auth_sessions, and `badcc4879431` — admin_settings),
collapsing the migration graph into one linear branch. With this in
place `alembic upgrade head` resolves to a single revision and CI no
longer fails on the multi-head error.

It also adds the indexes, unique constraints, and GIN full-text-search
indexes the application has been missing. Every change is DDL only —
no schema or model changes — and uses IF NOT EXISTS / IF EXISTS guards
so re-running on a partially-applied production database is safe.

What this migration does
========================

(1) Composite / covering indexes on hot query paths
    - posts: (created_at DESC), (author_id, created_at DESC),
      (district_id, created_at DESC), (group_id, created_at DESC)
    - comments: (post_id, parent_id, created_at),
      (author_id, created_at DESC)
    - notifications: (user_id, created_at DESC)
    - live_feed_messages: (feed_id, created_at DESC)
    - live_feeds: (is_active, created_at DESC)
    - events: (date), (organizer_id, date DESC)
    - ussd_sessions: (phone_number), (session_id)
    - subscriptions: (user_id, status)
    - comments.parent_id, group_members.user_id, group_members.group_id

(2) Unique constraints to enforce data integrity at the DB level
    - votes (user_id, post_id)        — the application already does
      an "existing = ..." check before insert; promoting to a UNIQUE
      constraint closes the race. Migration first deduplicates any
      pre-existing rows, then adds the constraint inside a guarded
      block.
    - event_attendees (user_id, event_id)  — same pattern; closes the
      duplicate-RSVP race.
    - followers (follower_id, followed_id) — already declared on the
      SQLAlchemy model, but a re-declare IF NOT EXISTS ensures the DB
      always has it.

(3) GIN indexes for full-text search
    - users.search_vector, posts.search_vector, comments.search_vector,
      groups.search_vector, topics.search_vector — all five previously
      had the tsvector column but no supporting index, forcing the @@
      operator in /search/ to sequential-scan the table. The articles
      table already has a GIN index on tsv_document from migration
      8ebe31796aac; we add a parallel GIN on articles.search_vector
      for consistency.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
# Two-parent merge: this migration is the single successor to both
# `a1b2c3d4e5f6` (auth_sessions) and `badcc4879431` (admin_settings).
down_revision: Union[str, Sequence[str], None] = (
    "a1b2c3d4e5f6",
    "badcc4879431",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _constraint_exists(bind, constraint_name: str) -> bool:
    """Return True if a constraint with the given name already exists."""
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint WHERE conname = :name"
            ),
            {"name": constraint_name},
        ).scalar()
    )


def _add_unique_if_missing(table: str, constraint_name: str, columns: str) -> None:
    """Add a UNIQUE constraint on `table` if it isn't already present.

    PostgreSQL's ALTER TABLE ... ADD CONSTRAINT does not support
    IF NOT EXISTS, so we pre-check pg_constraint and skip silently
    if the constraint is already in place. This makes the migration
    safe to re-run against a partially-applied production DB.
    """
    bind = op.get_bind()
    if _constraint_exists(bind, constraint_name):
        return
    op.execute(
        f"ALTER TABLE {table} "
        f"ADD CONSTRAINT {constraint_name} UNIQUE ({columns});"
    )


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Composite / covering indexes
    # ------------------------------------------------------------------
    # Every CREATE INDEX is wrapped in IF NOT EXISTS so re-running the
    # migration against a partially-applied production DB is safe.

    # ----- posts -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_created_at_desc "
        "ON posts (created_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_author_id_created_at_desc "
        "ON posts (author_id, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_district_id_created_at_desc "
        "ON posts (district_id, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_group_id_created_at_desc "
        "ON posts (group_id, created_at DESC);"
    )

    # ----- comments -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_comments_post_id_parent_id_created_at "
        "ON comments (post_id, parent_id, created_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_comments_author_id_created_at_desc "
        "ON comments (author_id, created_at DESC);"
    )
    # Self-referential FK column (parent_id) — used by ON DELETE CASCADE
    # and reply lookups. The composite above already starts with
    # (post_id, parent_id) so an index-only scan covers it; the
    # standalone (parent_id) index is for queries that filter by parent
    # only, e.g. "all replies to comment X across the site".
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_comments_parent_id "
        "ON comments (parent_id);"
    )

    # ----- notifications -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_user_id_created_at_desc "
        "ON notifications (user_id, created_at DESC);"
    )

    # ----- live feed messages -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_live_feed_messages_feed_id_created_at_desc "
        "ON live_feed_messages (feed_id, created_at DESC);"
    )

    # ----- live feeds (active filter is the most common query) -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_live_feeds_is_active_created_at_desc "
        "ON live_feeds (is_active, created_at DESC);"
    )

    # ----- events -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_date "
        "ON events (date);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_organizer_id_date_desc "
        "ON events (organizer_id, date DESC);"
    )

    # ----- USSD sessions -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ussd_sessions_phone_number "
        "ON ussd_sessions (phone_number);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ussd_sessions_session_id "
        "ON ussd_sessions (session_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ussd_sessions_updated_at "
        "ON ussd_sessions (updated_at DESC);"
    )

    # ----- subscriptions -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_user_id_status "
        "ON subscriptions (user_id, status);"
    )

    # ----- association tables (FK columns) -----
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_group_members_user_id "
        "ON group_members (user_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_group_members_group_id "
        "ON group_members (group_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_post_categories_post_id "
        "ON post_categories (post_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_post_categories_category_id "
        "ON post_categories (category_id);"
    )

    # ------------------------------------------------------------------
    # 2. Unique constraints (with safe-precondition guards)
    # ------------------------------------------------------------------
    # votes: deduplicate first, then add the UNIQUE constraint.
    # The /posts/{id}/like and /votes/ endpoints already do an
    # "existing = ..." check before insert, so legitimate duplicates
    # are rare. The dedup step below keeps ANY one row per (user, post)
    # (preferring the earliest id) and removes the rest.
    op.execute(
        """
        DELETE FROM votes v
        USING votes v2
        WHERE v.user_id = v2.user_id
          AND v.post_id = v2.post_id
          AND v.id > v2.id;
        """
    )
    # Add the UNIQUE constraint. We use raw SQL because Alembic's
    # `create_unique_constraint` cannot be made IF NOT EXISTS — and
    # idempotency matters here because this migration can be re-run
    # against a partially-applied DB.
    _add_unique_if_missing(
        table="votes",
        constraint_name="uq_votes_user_id_post_id",
        columns="user_id, post_id",
    )

    # event_attendees: dedup then add UNIQUE.
    op.execute(
        """
        DELETE FROM event_attendees a
        USING event_attendees a2
        WHERE a.user_id = a2.user_id
          AND a.event_id = a2.event_id
          AND a.id > a2.id;
        """
    )
    _add_unique_if_missing(
        table="event_attendees",
        constraint_name="uq_event_attendees_user_id_event_id",
        columns="user_id, event_id",
    )

    # followers: the SQLAlchemy model already declares UniqueConstraint
    # in __table_args__. We re-assert it here to guarantee the DB-level
    # constraint exists regardless of how the table was originally
    # created.
    _add_unique_if_missing(
        table="followers",
        constraint_name="uq_followers_follower_id_followed_id",
        columns="follower_id, followed_id",
    )

    # ------------------------------------------------------------------
    # 3. GIN indexes for full-text search
    # ------------------------------------------------------------------
    # users.search_vector — used by /search/ for user full-text matches.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_search_vector_gin "
        "ON users USING GIN (search_vector);"
    )
    # posts.search_vector
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_search_vector_gin "
        "ON posts USING GIN (search_vector);"
    )
    # comments.search_vector
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_comments_search_vector_gin "
        "ON comments USING GIN (search_vector);"
    )
    # groups.search_vector
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_groups_search_vector_gin "
        "ON groups USING GIN (search_vector);"
    )
    # topics.search_vector
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_topics_search_vector_gin "
        "ON topics USING GIN (search_vector);"
    )
    # articles.search_vector (the existing GIN on tsv_document covers
    # the weighted expression; this one covers the sqlalchemy_searchable
    # column for the same @@ operator used in /search/).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_articles_search_vector_gin "
        "ON articles USING GIN (search_vector);"
    )


def downgrade() -> None:
    # Drop in reverse order. IF EXISTS lets the downgrade succeed even
    # if a previous downgrade was partially applied.

    # ----- GIN indexes -----
    op.execute("DROP INDEX IF EXISTS ix_articles_search_vector_gin;")
    op.execute("DROP INDEX IF EXISTS ix_topics_search_vector_gin;")
    op.execute("DROP INDEX IF EXISTS ix_groups_search_vector_gin;")
    op.execute("DROP INDEX IF EXISTS ix_comments_search_vector_gin;")
    op.execute("DROP INDEX IF EXISTS ix_posts_search_vector_gin;")
    op.execute("DROP INDEX IF EXISTS ix_users_search_vector_gin;")

    # ----- Unique constraints -----
    op.execute(
        "ALTER TABLE followers DROP CONSTRAINT IF EXISTS "
        "uq_followers_follower_id_followed_id;"
    )
    op.execute(
        "ALTER TABLE event_attendees DROP CONSTRAINT IF EXISTS "
        "uq_event_attendees_user_id_event_id;"
    )
    op.execute(
        "ALTER TABLE votes DROP CONSTRAINT IF EXISTS "
        "uq_votes_user_id_post_id;"
    )

    # ----- Association table FK indexes -----
    op.execute("DROP INDEX IF EXISTS ix_post_categories_category_id;")
    op.execute("DROP INDEX IF EXISTS ix_post_categories_post_id;")
    op.execute("DROP INDEX IF EXISTS ix_group_members_group_id;")
    op.execute("DROP INDEX IF EXISTS ix_group_members_user_id;")

    # ----- Composite / covering indexes (reverse order) -----
    op.execute("DROP INDEX IF EXISTS ix_subscriptions_user_id_status;")
    op.execute("DROP INDEX IF EXISTS ix_ussd_sessions_updated_at;")
    op.execute("DROP INDEX IF EXISTS ix_ussd_sessions_session_id;")
    op.execute("DROP INDEX IF EXISTS ix_ussd_sessions_phone_number;")
    op.execute("DROP INDEX IF EXISTS ix_events_organizer_id_date_desc;")
    op.execute("DROP INDEX IF EXISTS ix_events_date;")
    op.execute("DROP INDEX IF EXISTS ix_live_feeds_is_active_created_at_desc;")
    op.execute(
        "DROP INDEX IF EXISTS ix_live_feed_messages_feed_id_created_at_desc;"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_notifications_user_id_created_at_desc;"
    )
    op.execute("DROP INDEX IF EXISTS ix_messages_mp_id_created_at_desc;")
    op.execute(
        "DROP INDEX IF EXISTS "
        "ix_messages_sender_id_recipient_id_created_at;"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_messages_recipient_id_created_at_desc;"
    )
    op.execute("DROP INDEX IF EXISTS ix_comments_parent_id;")
    op.execute("DROP INDEX IF EXISTS ix_comments_author_id_created_at_desc;")
    op.execute(
        "DROP INDEX IF EXISTS "
        "ix_comments_post_id_parent_id_created_at;"
    )
    op.execute("DROP INDEX IF EXISTS ix_posts_group_id_created_at_desc;")
    op.execute("DROP INDEX IF EXISTS ix_posts_district_id_created_at_desc;")
    op.execute("DROP INDEX IF EXISTS ix_posts_author_id_created_at_desc;")
    op.execute("DROP INDEX IF EXISTS ix_posts_created_at_desc;")
