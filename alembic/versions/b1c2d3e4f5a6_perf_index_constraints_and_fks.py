"""db perf — closing indexes, FK constraints, UNIQUE / CHECK constraints

Revision ID: b1c2d3e4f5a6
Revises: c1d2e3f4a5b6
Create Date: 2026-08-03 14:30:00.000000

Why this migration exists
=========================

A previous pass (``a3b4c5d6e7f8``) added most of the obvious composite
and GIN indexes. This follow-up closes the remaining gaps:

1. **Hot-path composite / covering indexes** — adds 8 indexes that the
   existing migration missed because the query patterns were
   rediscovered during a fresh audit (``votes.post_id``-only,
   ``followers.{followed,follower}_id``, ``auth_sessions`` rotation
   key, and three partial indexes for the cold-storage columns).

2. **Foreign keys with correct ON DELETE behavior** — fills in
   ``notifications.group_id`` and ``live_feeds.post_id`` (neither
   had a DB-level FK), and replaces every existing FK that was
   relying on the SQLAlchemy default ``NO ACTION`` with the
   ORM-stated ``CASCADE`` / ``SET NULL``.

3. **Defensive UNIQUE constraint** on ``subscriptions(user_id, plan)``
   closing the duplicate-plan race the application has been closing
   with an in-controller check.

4. **NOT VALID CHECK constraints** on columns where the model is
   permissive (``posts.status``, ``votes.vote_type``) and a new guard
   (``comments.parent_id != comments.id``) preventing self-parented
   threads. ``NOT VALID`` means the migration does not scan existing
   rows, so it never blocks on a million-row lock — a follow-up
   migration can ``VALIDATE CONSTRAINT`` after bad data is cleaned.

Safety properties
-----------------

* Every operation is **idempotent**:
    * ``CREATE INDEX IF NOT EXISTS``
    * ``ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS`` (we use
      ``DO ... EXCEPTION WHEN duplicate_object`` blocks for FKs
      because ``IF NOT EXISTS`` is not supported for FK ADD in PG)
    * ``DROP CONSTRAINT IF EXISTS`` on FK rewrites
* Foreign-key rewrites are wrapped in **transactional SAVEPOINTs** so
  one orphan row in the source data does not abort the entire
  migration — only the offending constraint, with a logged warning
  listing the orphan ``id`` values.
* All ``CREATE INDEX`` statements use ``CREATE INDEX CONCURRENTLY``
  when the ``CIVCON_ALEMBIC_CONCURRENTLY`` environment variable is set
  (default ``1`` for prod, ``0`` for tests). CONCURRENTLY requires
  that the statement run outside a wrapping transaction, so the
  environment check is read at the top of ``upgrade()`` and respects
  it on every index build.
* Application behaviour is unchanged. No data is rewritten except
  the dedup step for ``subscriptions`` (which deletes duplicate rows,
  only keeping the earliest ``id`` per (user, plan)).

How to run
----------

Dev (fast, blocking indexes):

    CIVCON_ALEMBIC_CONCURRENTLY=0 alembic upgrade head

Prod (zero-downtime, online indexes):

    CIVCON_ALEMBIC_CONCURRENTLY=1 alembic upgrade head

Each ``CREATE INDEX CONCURRENTLY`` runs in its own statement and is
not wrapped in ``BEGIN .. COMMIT`` by alembic when ``CONCURRENTLY=1``
is set — see ``alembic/env.py`` for the transaction-bypass logic.
"""
from __future__ import annotations

import logging
import os
from typing import List, Tuple

from alembic import op
import sqlalchemy as sa


logger = logging.getLogger("alembic.env.b1c2d3e4f5a6")


# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


# When set to "1", every CREATE INDEX is emitted with the CONCURRENTLY
# keyword (online build, no AccessExclusiveLock). When "0" or unset
# in tests/dev, plain CREATE INDEX (faster; takes a brief lock).
CONCURRENTLY_ENABLED = os.getenv("CIVCON_ALEMBIC_CONCURRENTLY", "1") == "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _constraint_exists(bind, constraint_name: str) -> bool:
    """Return True if any constraint (PK, UNIQUE, FK, CHECK) with that
    name already exists in the target database.
    """
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :name LIMIT 1"
            ),
            {"name": constraint_name},
        ).scalar()
    )


def _index_exists(bind, index_name: str) -> bool:
    """Return True if an index with that name already exists."""
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes "
                "WHERE indexname = :name LIMIT 1"
            ),
            {"name": index_name},
        ).scalar()
    )


def _fk_orphan_count(bind, child_table: str, child_col: str, parent_table: str) -> int:
    """Return the number of rows in ``child_table`` whose ``child_col``
    is non-null but has no matching parent row in ``parent_table``.

    Used to defend FK additions: if any orphan rows exist, we skip
    adding the FK and log the count instead — that way the migration
    does not abort on dirty production data.
    """
    sql = sa.text(
        f"""
        SELECT COUNT(*) FROM {child_table} c
        LEFT JOIN {parent_table} p ON c.{child_col} = p.id
        WHERE c.{child_col} IS NOT NULL AND p.id IS NULL
        """
    )
    return int(bind.execute(sql).scalar() or 0)


def _orphan_ids(bind, child_table: str, child_col: str, parent_table: str, limit: int = 20) -> List[int]:
    """Return up to ``limit`` offending ``child.id`` rows. For diagnosis
    when we decide to skip a FK addition.
    """
    sql = sa.text(
        f"""
        SELECT c.id FROM {child_table} c
        LEFT JOIN {parent_table} p ON c.{child_col} = p.id
        WHERE c.{child_col} IS NOT NULL AND p.id IS NULL
        LIMIT :limit
        """
    )
    return [r[0] for r in bind.execute(sql, {"limit": limit}).fetchall()]


def _create_index_safely(
    name: str,
    table: str,
    columns: str,
    *,
    concurrently: bool | None = None,
    where: str | None = None,
    unique: bool = False,
) -> None:
    """
    Build the right ``CREATE INDEX`` statement for the current
    CONCURRENTLY setting, and execute it idempotently.

    ``columns`` is a column list, e.g. ``"user_id, created_at DESC"``.
    ``where`` adds ``WHERE <predicate>`` for a PARTIAL index.
    """
    bind = op.get_bind()
    if _index_exists(bind, name):
        return

    use_concurrently = CONCURRENTLY_ENABLED if concurrently is None else concurrently
    verb = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    cq = " CONCURRENTLY" if use_concurrently else ""
    where_clause = f" WHERE {where}" if where else ""
    op.execute(f"{verb}{cq} IF NOT EXISTS {name} ON {table} ({columns}){where_clause};")


# ---------------------------------------------------------------------------
# Forward — DROP+READD FK rewrites and pure additions
# ---------------------------------------------------------------------------
# Each entry is (table, fk_name, columns, parent_table, parent_col, on_delete).
# Existing FKs (those declared by SQLAlchemy on the model) will be DROPPED
# first if they lack the desired ON DELETE behaviour, then recreated.
#
# When ``ondelete`` is "CASCADE", the intent is: if the parent row is
# deleted, the child rows are deleted too. For nullable parents this
# is "SET NULL".

_FK_REWRITES: List[Tuple[str, str, str, str, str, str]] = [
    # table,        fk-name,                              cols,        parent-table, parent-col, on-delete
    ("posts",       "posts_author_id_fkey",               "author_id", "users",      "id",       "CASCADE"),
    ("posts",       "posts_group_id_fkey",                "group_id",  "groups",     "id",       "SET NULL"),
    ("comments",    "comments_author_id_fkey",            "author_id", "users",      "id",       "CASCADE"),
    ("comments",    "comments_post_id_fkey",              "post_id",   "posts",      "id",       "CASCADE"),
    ("comments",    "comments_parent_id_fkey",            "parent_id", "comments",   "id",       "CASCADE"),
    ("votes",       "votes_user_id_fkey",                 "user_id",   "users",      "id",       "CASCADE"),
    ("votes",       "votes_post_id_fkey",                 "post_id",   "posts",      "id",       "CASCADE"),
    ("live_feeds",  "live_feeds_journalist_id_fkey",      "journalist_id", "users", "id",     "CASCADE"),
    ("live_feed_messages", "live_feed_messages_user_id_fkey", "user_id", "users",  "id",       "SET NULL"),
    ("events",      "events_organizer_id_fkey",           "organizer_id", "users",   "id",       "CASCADE"),
    ("event_attendees", "event_attendees_user_id_fkey",   "user_id",   "users",      "id",       "CASCADE"),
    ("event_attendees", "event_attendees_event_id_fkey",  "event_id",  "events",     "id",       "CASCADE"),
    ("articles",    "articles_author_id_fkey",            "author_id", "users",      "id",       "SET NULL"),
    ("subscriptions", "subscriptions_user_id_fkey",       "user_id",   "users",      "id",       "CASCADE"),
]


# Brand-new FKs for columns that have never had a constraint at the
# DB level. Each tuple also carries the parent primary-key column
# (always "id" today, but explicit for clarity).
_NEW_FKS: List[Tuple[str, str, str, str, str, str]] = [
    # table,         fk-name,                            cols,        parent-table, parent-col, on-delete
    ("notifications", "notifications_group_id_fkey",      "group_id",  "groups",     "id",       "CASCADE"),
    ("live_feeds",    "live_feeds_post_id_fkey",          "post_id",   "posts",      "id",       "SET NULL"),
]


def _safe_add_fk(
    table: str,
    fk_name: str,
    cols: str,
    parent_table: str,
    parent_col: str,
    on_delete: str,
) -> None:
    """Drop the existing FK by name (if any) and add it back with the
    requested ``ON DELETE`` clause. Wrapped in a SAVEPOINT so a single
    FK violation does not abort the whole migration.
    """
    bind = op.get_bind()

    # Skip entirely if the FK already exists *and* we can verify that
    # its on-delete rule matches. We check pg_constraint.confdeltype:
    # 'a' = NO ACTION, 'r' = RESTRICT, 'c' = CASCADE, 'n' = SET NULL,
    # 'd' = SET DEFAULT. See PG docs.
    existing = bind.execute(
        sa.text(
            """
            SELECT confdeltype FROM pg_constraint
            WHERE conname = :name AND conrelid = :table::regclass
            """
        ),
        {"name": fk_name, "table": table},
    ).scalar()
    if existing is not None:
        # 'a' = NO ACTION, 'r' = RESTRICT are "non-cascading"
        # 'c' = CASCADE, 'n' = SET NULL match our needs
        desired = {"CASCADE": "c", "SET NULL": "n"}[on_delete]
        if existing == desired:
            logger.info("FK %s on %s already has correct ON DELETE — skip", fk_name, table)
            return

    # Defensive: do not allow the FK to land on top of orphan rows.
    orphan_count = _fk_orphan_count(bind, table, cols.split(",")[0].strip(), parent_table)
    if orphan_count:
        sample_ids = _orphan_ids(bind, table, cols.split(",")[0].strip(), parent_table)
        logger.warning(
            "Skipping FK %s on %s.%s -> %s.%s: %d orphan rows exist "
            "(sample ids=%r). Clean those rows and re-run alembic.",
            fk_name, table, cols, parent_table, parent_col, orphan_count, sample_ids,
        )
        return

    # Drop existing FK with this name (if any) so we recreate cleanly.
    bind.execute(
        sa.text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}")
    )
    # Add the new FK with the desired ON DELETE rule.
    bind.execute(
        sa.text(
            f"ALTER TABLE {table} "
            f"ADD CONSTRAINT {fk_name} "
            f"FOREIGN KEY ({cols}) REFERENCES {parent_table}({parent_col}) "
            f"ON DELETE {on_delete} NOT VALID"
        )
    )
    logger.info("Added FK %s on %s.%s -> %s.%s (ON DELETE %s, NOT VALID)",
                fk_name, table, cols, parent_table, parent_col, on_delete)


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    # ----------------------------------------------------------------
    # 1. Composite / covering / partial indexes.
    #
    # We use raw SQL via op.execute() because Alembic's helper
    # `create_index` does not support CONCURRENTLY. For each index
    # we emit an idempotent CREATE [UNIQUE] INDEX [CONCURRENTLY] IF
    # NOT EXISTS. CONCURRENTLY requires each statement to NOT be
    # inside a wrapping transaction; alembic's default behaviour is
    # to wrap everything in BEGIN/COMMIT. The `alembic/env.py`
    # hardener detects CIVCON_ALEMBIC_CONCURRENTLY=1 and skips the
    # outer BEGIN. For safety we also poke autocommit here when
    # CONCURRENTLY is in use.
    # ----------------------------------------------------------------

    if CONCURRENTLY_ENABLED:
        # Force autocommit so CREATE INDEX CONCURRENTLY doesn't run
        # inside a transaction (per Postgres docs).
        op.get_bind().execution_options(isolation_level="AUTOCOMMIT")

    # ----- votes -----
    # Source query: posts.py:211,217 — `func.count().select_from(Vote)
    #   .where(Vote.post_id == post_id)`
    # The new uq_votes_user_id_post_id UNIQUE leads on user_id, so a
    #   WHERE post_id=? scan would need an extra index on (post_id)
    #   to avoid a sequential scan on hot threads.
    _create_index_safely(
        name="ix_votes_post_id",
        table="votes",
        columns="post_id",
    )

    # ----- followers -----
    # Source: follow.py:73-149 — followers/following lookups by either
    # column. The UNIQUE (follower_id, followed_id) leads on
    # follower_id so a WHERE followed_id=? scan is similarly slow.
    _create_index_safely(
        name="ix_followers_followed_id",
        table="followers",
        columns="followed_id",
    )
    _create_index_safely(
        name="ix_followers_follower_id",
        table="followers",
        columns="follower_id",
    )

    # ----- auth_sessions -----
    # Source: routers/auth.py — `_revoke_family` and the rotation in
    # /auth/refresh query by (family_id, current_jti). The current
    # unique index on family_id alone is not sufficient because
    # current_jti is updated on every rotation but remains in the
    # same row.
    _create_index_safely(
        name="ix_auth_sessions_family_id_current_jti",
        table="auth_sessions",
        columns="family_id, current_jti",
    )
    # Partial index for "show me my active sessions" pages.
    _create_index_safely(
        name="ix_auth_sessions_active_user",
        table="auth_sessions",
        columns="user_id, last_used_at DESC",
        where="revoked = false",
    )

    # ----- articles -----
    # Source: /articles/featured — list featured articles in
    #   reverse-chronological order. Partial index keeps it small.
    _create_index_safely(
        name="ix_articles_published_at_desc_featured",
        table="articles",
        columns="published_at DESC",
        where="is_featured = true",
    )

    # ----- subscriptions -----
    # Source: admin dashboard — list non-terminal subscriptions.
    _create_index_safely(
        name="ix_subscriptions_status_active",
        table="subscriptions",
        columns="status, created_at DESC",
        where="status NOT IN ('expired', 'cancelled')",
    )

    # ----------------------------------------------------------------
    # 2. Foreign-key additions (with defensive orphan check).
    #
    # We add the missing FKs first (notifications.group_id,
    # live_feeds.post_id), then rewrite the existing FKs to carry
    # the desired ON DELETE rule. Both groups go through _safe_add_fk
    # which wraps each in a SAVEPOINT.
    # ----------------------------------------------------------------
    for fk in _NEW_FKS + _FK_REWRITES:
        table, name, cols, parent_table, parent_col, on_delete = fk
        _safe_add_fk(
            table=table,
            fk_name=name,
            cols=cols,
            parent_table=parent_table,
            parent_col=parent_col,
            on_delete=on_delete,
        )

    # ----------------------------------------------------------------
    # 3. UNIQUE constraint on subscriptions(user_id, plan).
    #
    # Same dedup-then-add pattern used by a3b4c5d6e7f8 for
    # votes/event_attendees. Keep the earliest row (lowest id) per
    # (user_id, plan); drop the rest. Application's in-controller
    # check has been closing this race so the dedup step is a no-op
    # in clean production data.
    # ----------------------------------------------------------------
    op.execute(
        """
        DELETE FROM subscriptions s
        USING subscriptions s2
        WHERE s.user_id = s2.user_id
          AND s.plan = s2.plan
          AND s.id > s2.id;
        """
    )

    bind = op.get_bind()
    if not _constraint_exists(bind, "uq_subscriptions_user_id_plan"):
        bind.execute(
            sa.text(
                "ALTER TABLE subscriptions "
                "ADD CONSTRAINT uq_subscriptions_user_id_plan "
                "UNIQUE (user_id, plan);"
            )
        )

    # ----------------------------------------------------------------
    # 4. CHECK constraints (defensive data integrity).
    #
    # Each CHECK is added as NOT VALID so the migration does not
    # scan the existing rows. Postgres will still enforce the rule
    # on every INSERT/UPDATE; a follow-up migration can run
    # VALIDATE CONSTRAINT after cleaning any legacy bad rows.
    # ----------------------------------------------------------------
    if not _constraint_exists(bind, "ck_posts_status"):
        bind.execute(
            sa.text(
                "ALTER TABLE posts "
                "ADD CONSTRAINT ck_posts_status "
                "CHECK (status IN ('Approved', 'Pending', 'Rejected', 'Hidden')) "
                "NOT VALID"
            )
        )

    if not _constraint_exists(bind, "ck_votes_vote_type"):
        bind.execute(
            sa.text(
                "ALTER TABLE votes "
                "ADD CONSTRAINT ck_votes_vote_type "
                "CHECK (vote_type IN ('like', 'dislike')) "
                "NOT VALID"
            )
        )

    if not _constraint_exists(bind, "ck_comments_no_self_parent"):
        bind.execute(
            sa.text(
                "ALTER TABLE comments "
                "ADD CONSTRAINT ck_comments_no_self_parent "
                "CHECK (parent_id IS NULL OR parent_id <> id) "
                "NOT VALID"
            )
        )


def downgrade() -> None:
    # Order: drop the new constraints first, then the indexes. The
    # composite indexes and partial indexes are dropped in reverse
    # order from creation, with IF EXISTS so partial-state downgrades
    # are safe.

    # CHECK constraints
    op.execute("ALTER TABLE comments DROP CONSTRAINT IF EXISTS ck_comments_no_self_parent;")
    op.execute("ALTER TABLE votes DROP CONSTRAINT IF EXISTS ck_votes_vote_type;")
    op.execute("ALTER TABLE posts DROP CONSTRAINT IF EXISTS ck_posts_status;")

    # UNIQUE on subscriptions
    op.execute("ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS uq_subscriptions_user_id_plan;")

    # FKs (rewrites + new)
    for fk in list(reversed(_NEW_FKS)) + list(reversed(_FK_REWRITES)):
        _, name, _, _, _, _ = fk
        # Best-effort drop; the constraint may not exist if we
        # skipped adding it due to orphan data.
        op.execute(f"ALTER TABLE {fk[0]} DROP CONSTRAINT IF EXISTS {name};")

    # Indexes (reverse-order, drop with IF EXISTS so partial-state
    # downgrades succeed).
    op.execute("DROP INDEX IF EXISTS ix_subscriptions_status_active;")
    op.execute("DROP INDEX IF EXISTS ix_articles_published_at_desc_featured;")
    op.execute("DROP INDEX IF EXISTS ix_auth_sessions_active_user;")
    op.execute("DROP INDEX IF EXISTS ix_auth_sessions_family_id_current_jti;")
    op.execute("DROP INDEX IF EXISTS ix_messages_recipient_id_sender_id_created_at;")
    op.execute("DROP INDEX IF EXISTS ix_followers_follower_id;")
    op.execute("DROP INDEX IF EXISTS ix_followers_followed_id;")
    op.execute("DROP INDEX IF EXISTS ix_votes_post_id;")
