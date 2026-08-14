"""create case core tables (cases, attachments, responses) + case_number_seq

Revision ID: c2b3c4d5e6f7
Revises: c1a2b3d4e5f6
Create Date: 2026-08-05 09:30:00.000000

This is migration 2 of 3 for the case-management domain. It creates:

- cases               — the central Case row
- case_attachments    — evidence attached to a Case
- case_responses      — messages in a Case's conversation thread
- case_number_seq     — Postgres SEQUENCE for race-safe case-number
                        generation (used by
                        app/services/cases/numbers.py)

Stage 3 (c3c4d5e6f7g8) creates the timeline + audit log +
assignments + support tables + the append-only trigger.

Notes on the indexes:

- `cases.case_number` is UNIQUE per spec STEP 3 (sequential, unique).
- The partial unique index on `is_anonymous` reporter is NOT yet
  enforced here — the column is just a flag. The future privacy
  hardening pass may add a CHECK constraint.
- The GIN index on `cases.search_vector` mirrors the existing
  Post.search_vector pattern (already in `models.py`).
- The case_number_seq is NOT owned by the cases table; the nextval()
  call site is the service layer. This makes it easy to reason about
  the invariant "numbers are unique, not gap-free".
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2b3c4d5e6f7'
down_revision = 'c1a2b3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # ------------------------------------------------------------------
    # cases
    # ------------------------------------------------------------------
    op.create_table(
        'cases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_number', sa.String(length=32), nullable=False),
        sa.Column('reporter_user_id', sa.Integer(), nullable=True),
        sa.Column(
            'display_handle',
            sa.String(length=120),
            nullable=False,
            server_default='Anonymous Citizen',
        ),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('district_id', sa.String(length=80), nullable=True),
        sa.Column(
            'priority',
            sa.Enum(
                'low', 'normal', 'high', 'critical',
                name='case_priority_enum',
            ),
            nullable=False,
            server_default='normal',
        ),
        sa.Column(
            'status',
            sa.Enum(
                'submitted', 'received', 'assigned', 'under_review',
                'information_requested', 'citizen_responded',
                'in_progress', 'resolved', 'closed',
                'withdrawn', 'rejected',
                name='case_status_enum',
            ),
            nullable=False,
            server_default='submitted',
        ),
        sa.Column('assigned_mp_profile_id', sa.Integer(), nullable=True),
        sa.Column(
            'is_anonymous',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
        sa.Column(
            'language',
            sa.String(length=8),
            nullable=False,
            server_default='EN',
        ),
        sa.Column(
            'search_vector',
            sa.dialects.postgresql.TSVECTOR(),
            nullable=True,
        ),
        sa.Column(
            'submitted_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['reporter_user_id'], ['users.id'], ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['category_id'], ['case_categories.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['assigned_mp_profile_id'], ['mp_profiles.id'], ondelete='SET NULL',
        ),
        sa.UniqueConstraint('case_number', name='uq_cases_case_number'),
    )

    # B-tree indexes
    op.create_index('ix_cases_id', 'cases', ['id'])
    op.create_index('ix_cases_case_number', 'cases', ['case_number'])
    op.create_index('ix_cases_reporter_user_id', 'cases', ['reporter_user_id'])
    op.create_index('ix_cases_category_id', 'cases', ['category_id'])
    op.create_index('ix_cases_status', 'cases', ['status'])
    op.create_index('ix_cases_priority', 'cases', ['priority'])
    op.create_index(
        'ix_cases_assigned_mp_profile_id', 'cases', ['assigned_mp_profile_id'],
    )
    op.create_index('ix_cases_district_id', 'cases', ['district_id'])

    # GIN index for full-text search (matches the existing
    # Post.search_vector pattern; the Post and Article tables already
    # have a similar GIN index).
    op.execute(
        "CREATE INDEX ix_cases_search_vector ON cases USING GIN (search_vector)"
    )

    # ------------------------------------------------------------------
    # case_attachments
    # ------------------------------------------------------------------
    op.create_table(
        'case_attachments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('media_url', sa.Text(), nullable=False),
        sa.Column(
            'media_type',
            sa.String(length=64),
            nullable=False,
            server_default='image',
        ),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('uploaded_by_id', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['case_id'], ['cases.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['uploaded_by_id'], ['users.id'], ondelete='SET NULL',
        ),
    )
    op.create_index('ix_case_attachments_id', 'case_attachments', ['id'])
    op.create_index('ix_case_attachments_case_id', 'case_attachments', ['case_id'])

    # ------------------------------------------------------------------
    # case_responses
    # ------------------------------------------------------------------
    op.create_table(
        'case_responses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('author_user_id', sa.Integer(), nullable=True),
        sa.Column('author_role', sa.String(length=32), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column(
            'is_internal',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['case_id'], ['cases.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['author_user_id'], ['users.id'], ondelete='SET NULL',
        ),
        sa.UniqueConstraint('id', name='uq_case_responses_id'),
    )
    op.create_index('ix_case_responses_id', 'case_responses', ['id'])
    op.create_index(
        'ix_case_responses_case_id', 'case_responses', ['case_id'],
    )

    # ------------------------------------------------------------------
    # case_number_seq — Postgres SEQUENCE for race-safe case numbers
    # ------------------------------------------------------------------
    # Uses AS [data_type] because the nextval() call site may want a
    # BIGINT (the sequence is 64-bit by default — wider than what we
    # format into 6 digits, easy to extend later).
    op.execute(
        "CREATE SEQUENCE case_number_seq AS bigint START WITH 1 INCREMENT BY 1"
    )


def downgrade():
    # Drop in reverse FK order.
    op.execute("DROP SEQUENCE IF EXISTS case_number_seq")

    op.drop_index('ix_case_responses_case_id', table_name='case_responses')
    op.drop_index('ix_case_responses_id', table_name='case_responses')
    op.drop_table('case_responses')

    op.drop_index('ix_case_attachments_case_id', table_name='case_attachments')
    op.drop_index('ix_case_attachments_id', table_name='case_attachments')
    op.drop_table('case_attachments')

    op.execute("DROP INDEX IF EXISTS ix_cases_search_vector")
    op.drop_index('ix_cases_district_id', table_name='cases')
    op.drop_index('ix_cases_assigned_mp_profile_id', table_name='cases')
    op.drop_index('ix_cases_priority', table_name='cases')
    op.drop_index('ix_cases_status', table_name='cases')
    op.drop_index('ix_cases_category_id', table_name='cases')
    op.drop_index('ix_cases_reporter_user_id', table_name='cases')
    op.drop_index('ix_cases_case_number', table_name='cases')
    op.drop_index('ix_cases_id', table_name='cases')
    op.drop_table('cases')

    # The Postgres Enum types created by the table definition are
    # dropped automatically when the table is dropped. No explicit
    # DROP TYPE needed.
