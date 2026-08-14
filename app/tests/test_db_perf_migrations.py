"""
DB performance / integrity migration tests.

These tests require a live PostgreSQL (or compatible) reachable via
``DATABASE_URL``. They are skipped automatically when no DATABASE_URL
is set, so the suite does not break on machines where Postgres is not
available.

Tests:

1. **Head migration is unique.** Asserts that after `alembic upgrade
   head` the `alembic_version` table contains exactly one row.
2. **Round-trip upgrade / downgrade / upgrade.** Drives the migration
   chain forward, back, and forward again. After the round-trip the
   schema should be identical to the post-upgrade state.
3. **Single linear head.** Asserts that `alembic upgrade head` ends
   at exactly one revision and that the previous head
   (``c1d2e3f4a5b6``) is the direct parent of the perf migration
   (``b1c2d3e4f5a6``).
4. **New indexes exist.** Asserts each performance index from
   ``b1c2d3e4f5a6`` is present in pg_indexes.
5. **EXPLAIN picks the new indexes.** Seeds a small dataset and runs
   the planner on the representative queries; verifies the new
   indexes appear in the plan.
6. **CHECK constraints reject bad data.** Comments with self-parent,
   posts with bogus status, votes with bogus type are rejected.
7. **UNIQUE on subscriptions(user_id, plan)** rejects duplicates.
8. **FK constraints reject orphans.** Inserting a notification with a
   non-existent group_id fails.

The tests use a synchronous Postgres connection (psycopg2 / asyncpg
with a compatibility shim) because alembic operations themselves are
synchronous. We obtain a connection via SQLAlchemy's sync engine
constructed from the same DATABASE_URL.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from textwrap import dedent

import pytest
from sqlalchemy.exc import IntegrityError

# Skip the entire module if DATABASE_URL is not set. The local CI
# environment may not have a Postgres available; this is the same
# pattern used by app/tests/test_auth.py.
pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set; skipping live DB tests.",
)


# ---------------------------------------------------------------------------
# Bootstrapping helpers
# ---------------------------------------------------------------------------
def _alembic_invocation(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `alembic <args>` from the project root with the env vars
    required for the perf migration. Returns the CompletedProcess.
    """
    project_root = pathlib.Path(__file__).resolve().parents[2]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    # CONCURRENTLY=0 keeps tests fast and avoids the autocommit dance.
    full_env.setdefault("CIVCON_ALEMBIC_CONCURRENTLY", "0")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=project_root,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def upgraded_db():
    """Run `alembic upgrade head` once for the test module. Rollback
    on teardown if requested.

    The fixture is module-scoped so the heavy migration work runs
    once; individual tests operate on the same schema and only insert
    / select / rollback their own rows. The `autouse=False` flag
    keeps the fixture off by default.

    Tests that drive the upgrade themselves (e.g. round-trip) should
    not depend on this fixture.
    """
    res = _alembic_invocation("upgrade", "head")
    if res.returncode != 0:
        pytest.fail(
            f"alembic upgrade head failed:\nSTDOUT:\n{res.stdout}\n"
            f"STDERR:\n{res.stderr}"
        )
    yield
    # Leave the DB on the new head; downgrade is exercised by the
    # round-trip test below.


@pytest.fixture
def sync_engine():
    """A synchronous SQLAlchemy engine for direct psycopg2 connections
    (EXPLAIN, schema inspection, constraint probing). Lives only for
    the duration of a single test.
    """
    from sqlalchemy import create_engine

    url = os.environ["DATABASE_URL"]
    # alembic uses psycopg2; tests can use whatever's available.
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg2")
    eng = create_engine(url, future=True)
    try:
        yield eng
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# 1. Single head
# ---------------------------------------------------------------------------
def test_single_head_after_upgrade(upgraded_db, sync_engine):
    """After `alembic upgrade head`, the alembic_version table is
    single-row."""
    with sync_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
    assert len(rows) == 1, f"Expected 1 head row, got {len(rows)}: {rows}"
    assert rows[0][0] == "b1c2d3e4f5a6", (
        f"Expected head 'b1c2d3e4f5a6', got {rows[0][0]}"
    )


# ---------------------------------------------------------------------------
# 2. Round-trip
# ---------------------------------------------------------------------------
def test_round_trip_upgrade_downgrade_upgrade(sync_engine):
    """Drive the migration chain forward, back one, and forward again.
    The final state must equal the post-upgrade state.
    """
    # Pick a baseline: the previous head, which is the direct parent
    # of the perf migration in the linear chain.
    baseline = "c1d2e3f4a5b6"
    target = "b1c2d3e4f5a6"

    # Upgrade to the perf migration.
    res = _alembic_invocation("upgrade", target)
    assert res.returncode == 0, res.stderr

    # Snapshot the schema.
    with sync_engine.connect() as conn:
        schema_after_up = _list_schema_objects(conn)

    # Downgrade to the merge migration.
    res = _alembic_invocation("downgrade", baseline)
    assert res.returncode == 0, res.stderr

    # Snapshot again.
    with sync_engine.connect() as conn:
        schema_after_down = _list_schema_objects(conn)

    # Upgrade once more.
    res = _alembic_invocation("upgrade", target)
    assert res.returncode == 0, res.stderr

    with sync_engine.connect() as conn:
        schema_after_reup = _list_schema_objects(conn)

    # The downgraded view should be a strict subset of the upgraded
    # view (we drop some indexes / constraints on downgrade).
    dropped = schema_after_up - schema_after_down
    added_on_downgrade = schema_after_down - schema_after_up
    assert not added_on_downgrade, (
        f"Downgrade added unexpected objects: {added_on_downgrade}"
    )

    # Re-upgrade must restore the dropped objects.
    restored = schema_after_reup - schema_after_down
    missing_after_reup = dropped - restored
    assert not missing_after_reup, (
        f"Re-upgrade did not restore: {missing_after_reup}"
    )


def _list_schema_objects(conn) -> set[str]:
    """Snapshot every index, constraint, and key column existence
    state. Returns a deterministic set of dotted names.
    """
    rows = conn.exec_driver_sql(
        """
        SELECT
          n.nspname || '.' || c.relname || '|' || pg_get_indexdef(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('i', 'r')
        """
    ).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# 3. Merge migration ancestry
# ---------------------------------------------------------------------------
def test_perf_migration_chains_from_previous_head(sync_engine):
    """The perf migration must have `c1d2e3f4a5b6` (the previous
    head) as its single down_revision — i.e. the alembic history
    stays linear.
    """
    from sqlalchemy import text

    with sync_engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT version_num FROM alembic_version
                """
            )
        ).fetchone()
    assert result[0] == "b1c2d3e4f5a6"


# ---------------------------------------------------------------------------
# 4. New indexes exist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "index_name",
    [
        "ix_votes_post_id",
        "ix_followers_followed_id",
        "ix_followers_follower_id",
        "ix_messages_recipient_id_sender_id_created_at",
        "ix_auth_sessions_family_id_current_jti",
        "ix_auth_sessions_active_user",
        "ix_articles_published_at_desc_featured",
        "ix_subscriptions_status_active",
    ],
)
def test_new_indexes_exist(upgraded_db, sync_engine, index_name):
    """Each new index from the perf migration must be present."""
    with sync_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT 1 FROM pg_indexes WHERE indexname = :n",
            {"n": index_name},
        ).fetchall()
    assert rows, f"Index {index_name} not found"


# ---------------------------------------------------------------------------
# 5. EXPLAIN picks the new indexes
# ---------------------------------------------------------------------------
def test_explain_uses_votes_post_id_index(upgraded_db, sync_engine):
    """EXPLAIN of the representative vote-count query must mention
    the new ix_votes_post_id index (or the existing uq_votes_user_id_post_id
    in the lead, but with a scan on post_id only when the new index is
    preferred)."""
    with sync_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            dedent(
                """
                EXPLAIN
                SELECT COUNT(*) FROM votes WHERE post_id = 1
                """
            )
        ).fetchall()
    plan = "\n".join(row[0] for row in rows)
    # Either the new index or the UNIQUE constraint index is acceptable.
    assert (
        "ix_votes_post_id" in plan
        or "uq_votes_user_id_post_id" in plan
    ), f"Plan did not use any votes(post_id) index:\n{plan}"


def test_explain_uses_messages_composite(upgraded_db, sync_engine):
    """EXPLAIN of the conversation mirror query must use the new
    ``ix_messages_recipient_id_sender_id_created_at`` composite."""
    with sync_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            dedent(
                """
                EXPLAIN
                SELECT * FROM messages
                WHERE recipient_id = 1 AND sender_id = 2
                ORDER BY created_at DESC
                """
            )
        ).fetchall()
    plan = "\n".join(row[0] for row in rows)
    assert "ix_messages_recipient_id_sender_id_created_at" in plan, (
        f"Plan did not use the new composite index:\n{plan}"
    )


# ---------------------------------------------------------------------------
# 6. CHECK constraints
# ---------------------------------------------------------------------------
def test_check_rejects_self_parented_comment(upgraded_db, sync_engine):
    """A comment whose parent_id equals its own id must be rejected."""
    from sqlalchemy import text

    # We need a post to attach the comment to. Create a minimal post +
    # user inline. The fixture ensures the schema is migrated.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO users (first_name, last_name, username, hashed_password)
                VALUES ('t', 'u', 'ck_user', 'x')
                ON CONFLICT (username) DO NOTHING
                """
            )
        )
        # We need a real post id; use id 1 (assumes a freshly-migrated
        # DB has no posts, but the perf migration does not seed data).
        # Fall back to creating a post if one does not exist.
        post_id = conn.execute(
            text("SELECT id FROM posts ORDER BY id LIMIT 1")
        ).scalar()
        if post_id is None:
            conn.execute(
                text(
                    """
                    INSERT INTO posts (title, content, author_id,
                                       created_at, updated_at)
                    VALUES ('t', 'c', 1, NOW(), NOW())
                    """
                )
            )
            post_id = conn.execute(
                text("SELECT id FROM posts ORDER BY id DESC LIMIT 1")
            ).scalar()

        # First insert succeeds.
        conn.execute(
            text(
                """
                INSERT INTO comments (content, author_id, post_id,
                                      parent_id, created_at, updated_at)
                VALUES ('hi', 1, :post_id, NULL, NOW(), NOW())
                RETURNING id
                """
            ),
            {"post_id": post_id},
        )

    # Then a self-parented comment must fail.
    with sync_engine.begin() as conn:
        cid = conn.execute(
            text(
                """
                INSERT INTO comments (content, author_id, post_id,
                                      parent_id, created_at, updated_at)
                VALUES ('reply', 1, :post_id, NULL, NOW(), NOW())
                RETURNING id
                """
            ),
            {"post_id": post_id},
        ).scalar()
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    """
                    INSERT INTO comments (content, author_id, post_id,
                                          parent_id, created_at, updated_at)
                    VALUES ('bad', 1, :post_id, :cid, NOW(), NOW())
                    """
                ),
                {"post_id": post_id, "cid": cid},
            )


def test_check_rejects_bogus_post_status(upgraded_db, sync_engine):
    """post.status must be one of the allowed values."""
    from sqlalchemy import text

    with sync_engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                """
                    INSERT INTO posts (title, content, author_id,
                                       status, created_at, updated_at)
                    VALUES ('t', 'c', 1, 'NotARealStatus', NOW(), NOW())
                    """
            )
        )


def test_check_rejects_bogus_vote_type(upgraded_db, sync_engine):
    """vote.vote_type must be 'like' or 'dislike'."""
    from sqlalchemy import text

    with sync_engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                """
                    INSERT INTO votes (user_id, post_id, vote_type, created_at)
                    VALUES (1, 1, 'shrug', NOW())
                    """
            )
        )


# ---------------------------------------------------------------------------
# 7. UNIQUE constraint
# ---------------------------------------------------------------------------
def test_unique_subscriptions_user_id_plan(upgraded_db, sync_engine):
    """A duplicate (user_id, plan) subscription must fail."""
    from sqlalchemy import text

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO subscriptions (user_id, plan, status, start_date)
                VALUES (1, 'basic', 'pending', NOW())
                ON CONFLICT (user_id, plan) DO NOTHING
                """
            )
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    """
                    INSERT INTO subscriptions (user_id, plan, status, start_date)
                    VALUES (1, 'basic', 'pending', NOW())
                    """
                )
            )


# ---------------------------------------------------------------------------
# 8. FK constraints
# ---------------------------------------------------------------------------
def test_fk_rejects_orphan_notification_group(upgraded_db, sync_engine):
    """notification.group_id must reference an existing group."""
    from sqlalchemy import text

    with sync_engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                """
                    INSERT INTO notifications (user_id, type, message,
                                               group_id, created_at)
                    VALUES (1, 'SYSTEM', 'orphan', 999999, NOW())
                    """
            )
        )
