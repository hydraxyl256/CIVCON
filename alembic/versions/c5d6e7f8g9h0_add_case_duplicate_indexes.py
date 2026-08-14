"""add indexes for case duplicate detection (pg_trgm + partial open-status)

Revision ID: c5d6e7f8g9h0
Revises: c4d5e6f7g8h9
Create Date: 2026-08-05 18:00:00.000000

Adds the database-side support for the duplicate-detection service
(`app/services/cases/duplicates.py`).

Three things are installed:

  1. `pg_trgm` extension — enables `similarity()` and `gin_trgm_ops`.
     Required for typo-tolerant matching ("healt" vs "health"). The
     extension is treated as a hard prerequisite; if it is already
     installed by another migration / DBA setup, the `IF NOT EXISTS`
     guard makes this a no-op.

  2. `ix_cases_open_submitted_desc` — partial B-tree index on
     `(submitted_at DESC) WHERE status NOT IN ('withdrawn',
     'rejected', 'closed')`. The duplicate-check query filters by
     `status NOT IN (...) AND submitted_at >= window` — this index
     lets the planner skip past terminal rows AND past the
     window boundary without a full scan.

  3. `ix_cases_title_desc_trgm` — GIN index on
     `(title || ' ' || description) gin_trgm_ops`. Speeds up the
     `similarity(title || ' ' || description, query) > threshold`
     predicate in the WHERE clause.

Note: `ix_cases_search_vector` (GIN on `cases.search_vector`) is
ALREADY installed by migration `c2b3c4d5e6f7` and is NOT recreated
here. The migration only adds what the new service needs.

DOWNGRADE

  Reverses in strict reverse order: drop the GIN index, drop the
  partial B-tree, then drop the extension. The `DROP EXTENSION` is
  guarded with `IF EXISTS` so the downgrade does not fail when other
  objects in the schema depend on pg_trgm (e.g. a future PR adds
  trigram indexes for `posts`).
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'c5d6e7f8g9h0'
down_revision = 'c4d5e6f7g8h9'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Trigram extension — idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2. Partial B-tree for "open recent submissions" filter.
    #    The duplicate query's WHERE clause is:
    #       status NOT IN (...) AND submitted_at >= ...
    #    The partial predicate excludes terminal rows at index time.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_cases_open_submitted_desc
        ON cases (submitted_at DESC)
        WHERE status NOT IN ('withdrawn', 'rejected', 'closed')
        """
    )

    # 3. Trigram GIN over title + description. Used by:
    #    similarity(c.title || ' ' || c.description, :q) > 0.2
    # The expression index keeps the trigram tokens in sync with the
    # underlying text columns; the planner uses it whenever the WHERE
    # clause references the same expression.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_cases_title_desc_trgm
        ON cases USING GIN ((title || ' ' || description) gin_trgm_ops)
        """
    )


def downgrade():
    # Drop in reverse order. Extension drop is guarded.
    op.execute("DROP INDEX IF EXISTS ix_cases_title_desc_trgm")
    op.execute("DROP INDEX IF EXISTS ix_cases_open_submitted_desc")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")