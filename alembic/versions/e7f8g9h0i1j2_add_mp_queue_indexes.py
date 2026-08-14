"""add indexes for MP queue list queries

Revision ID: e7f8g9h0i1j2
Revises: d6e7f8g9h0i1
Create Date: 2026-08-06 10:00:00.000000

Adds two composite indexes that the MP queue endpoint
(`GET /cases/mp/queue`) needs to scan efficiently:

  - `ix_case_assignments_mp_active_assigned` — partial composite for
    the queue WHERE clause
        WHERE mp_profile_id = ? AND unassigned_at IS NULL
        ORDER BY submitted_at DESC
    The partial predicate matches the spec's "active assignment only"
    contract from `app/services/cases/assignments.py`.

  - `ix_cases_open_priority_submitted` — open-case forward index keyed
    on (status, priority DESC, submitted_at DESC). Covers the open-case
    list path used by the case-detail page query plan and any future
    MP-side filters.

The CaseAssignment partial unique index from c3c4d5e6f7g8 is retained
for the "at most one active assignment per case" contract; this new
index is its read-side complement.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "e7f8g9h0i1j2"
down_revision = "d6e7f8g9h0i1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MP queue read-side index — partial composite keyed on the
    # active-assignment predicate that the queue endpoint uses.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_case_assignments_mp_active_assigned "
        "ON case_assignments (mp_profile_id, assigned_at DESC) "
        "WHERE unassigned_at IS NULL"
    )

    # Forward index for the open-case list path (status filter +
    # priority sort + submitted_at sort). The DESC on `priority` is a
    # # Postgres lets DESC/ASC be applied to indexes directly.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cases_open_priority_submitted "
        "ON cases (status, priority DESC, submitted_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_case_assignments_mp_active_assigned")
    op.execute("DROP INDEX IF EXISTS ix_cases_open_priority_submitted")
