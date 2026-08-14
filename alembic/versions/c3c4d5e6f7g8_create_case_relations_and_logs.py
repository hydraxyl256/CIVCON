"""create case timeline, audit log, assignments, support + append-only trigger

Revision ID: c3c4d5e6f7g8
Revises: c2b3c4d5e6f7
Create Date: 2026-08-05 10:00:00.000000

This is migration 3 of 3 for the case-management domain. It creates:

- case_timeline      — public-facing timeline of case events
- case_audit_log     — append-only security audit trail
- case_assignments   — MP <-> case assignment history (with a partial
                       unique index enforcing "at most one active
                       assignment per case")
- case_support       — duplicate-case support records

It also installs the Postgres trigger `case_append_only()` that blocks
UPDATE and DELETE on `case_audit_log` and `case_timeline`. This is the
defense-in-depth layer for the append-only invariant — the service
layer only enqueues writes, but the trigger stops even a stray SQL
command from tampering.

The partial unique indexes:

  - case_assignments: at most one row per case where unassigned_at IS NULL
  - case_support:     at most one row per (original, duplicate) where
                       supporter_user_id IS NOT NULL

are declared via op.execute() because SQLAlchemy's UniqueConstraint
does not expose postgresql_where on generic DDL.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3c4d5e6f7g8'
down_revision = 'c2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    # ------------------------------------------------------------------
    # case_timeline
    # ------------------------------------------------------------------
    op.create_table(
        'case_timeline',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('from_status', sa.String(length=32), nullable=True),
        sa.Column('to_status', sa.String(length=32), nullable=True),
        sa.Column('actor_role', sa.String(length=32), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
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
            ['actor_user_id'], ['users.id'], ondelete='SET NULL',
        ),
    )
    op.create_index('ix_case_timeline_id', 'case_timeline', ['id'])
    op.create_index('ix_case_timeline_case_id', 'case_timeline', ['case_id'])

    # ------------------------------------------------------------------
    # case_audit_log
    # ------------------------------------------------------------------
    op.create_table(
        'case_audit_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('actor_role', sa.String(length=32), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('ip_address', sa.String(length=64), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=True),
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
            ['actor_user_id'], ['users.id'], ondelete='SET NULL',
        ),
    )
    op.create_index('ix_case_audit_log_id', 'case_audit_log', ['id'])
    op.create_index(
        'ix_case_audit_log_case_id', 'case_audit_log', ['case_id'],
    )
    op.create_index(
        'ix_case_audit_log_actor_user_id', 'case_audit_log', ['actor_user_id'],
    )
    op.create_index(
        'ix_case_audit_log_request_id', 'case_audit_log', ['request_id'],
    )

    # ------------------------------------------------------------------
    # case_assignments
    # ------------------------------------------------------------------
    op.create_table(
        'case_assignments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('mp_profile_id', sa.Integer(), nullable=False),
        sa.Column(
            'assigned_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
        sa.Column('assigned_by_user_id', sa.Integer(), nullable=True),
        sa.Column('unassigned_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['case_id'], ['cases.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['mp_profile_id'], ['mp_profiles.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['assigned_by_user_id'], ['users.id'], ondelete='SET NULL',
        ),
    )
    op.create_index(
        'ix_case_assignments_id', 'case_assignments', ['id'],
    )
    op.create_index(
        'ix_case_assignments_mp_profile_id', 'case_assignments', ['mp_profile_id'],
    )
    # Partial unique index: at most one active (unassigned_at IS NULL)
    # assignment per case.
    op.execute(
        "CREATE UNIQUE INDEX uq_case_assignments_active "
        "ON case_assignments (case_id) WHERE unassigned_at IS NULL"
    )

    # ------------------------------------------------------------------
    # case_support
    # ------------------------------------------------------------------
    op.create_table(
        'case_support',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('original_case_id', sa.Integer(), nullable=False),
        sa.Column('duplicate_case_id', sa.Integer(), nullable=True),
        sa.Column('supporter_user_id', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['original_case_id'], ['cases.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['duplicate_case_id'], ['cases.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['supporter_user_id'], ['users.id'], ondelete='SET NULL',
        ),
    )
    op.create_index('ix_case_support_id', 'case_support', ['id'])
    op.create_index(
        'ix_case_support_original_case_id', 'case_support', ['original_case_id'],
    )
    op.create_index(
        'ix_case_support_duplicate_case_id', 'case_support', ['duplicate_case_id'],
    )
    # Partial unique index: at most one (original, duplicate) pair
    # where supporter_user_id IS NOT NULL (anonymous duplicate
    # suppressions don't count).
    op.execute(
        "CREATE UNIQUE INDEX uq_case_support_pair "
        "ON case_support (original_case_id, duplicate_case_id) "
        "WHERE supporter_user_id IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # Append-only trigger for case_audit_log + case_timeline
    # ------------------------------------------------------------------
    # The trigger function raises on UPDATE / DELETE so a stray SQL
    # command (even outside the service layer) cannot tamper with the
    # audit trail or the public timeline.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION case_append_only() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'case_audit_log and case_timeline are append-only';
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER case_audit_log_append_only
            BEFORE UPDATE OR DELETE ON case_audit_log
            FOR EACH ROW EXECUTE FUNCTION case_append_only();
        """
    )
    op.execute(
        """
        CREATE TRIGGER case_timeline_append_only
            BEFORE UPDATE OR DELETE ON case_timeline
            FOR EACH ROW EXECUTE FUNCTION case_append_only();
        """
    )


def downgrade():
    # Drop triggers first, then the tables in reverse FK order.
    op.execute("DROP TRIGGER IF EXISTS case_timeline_append_only ON case_timeline")
    op.execute("DROP TRIGGER IF EXISTS case_audit_log_append_only ON case_audit_log")
    op.execute("DROP FUNCTION IF EXISTS case_append_only()")

    op.execute("DROP INDEX IF EXISTS uq_case_support_pair")
    op.drop_index('ix_case_support_duplicate_case_id', table_name='case_support')
    op.drop_index('ix_case_support_original_case_id', table_name='case_support')
    op.drop_index('ix_case_support_id', table_name='case_support')
    op.drop_table('case_support')

    op.execute("DROP INDEX IF EXISTS uq_case_assignments_active")
    op.drop_index('ix_case_assignments_mp_profile_id', table_name='case_assignments')
    op.drop_index('ix_case_assignments_id', table_name='case_assignments')
    op.drop_table('case_assignments')

    op.drop_index('ix_case_audit_log_request_id', table_name='case_audit_log')
    op.drop_index('ix_case_audit_log_actor_user_id', table_name='case_audit_log')
    op.drop_index('ix_case_audit_log_case_id', table_name='case_audit_log')
    op.drop_index('ix_case_audit_log_id', table_name='case_audit_log')
    op.drop_table('case_audit_log')

    op.drop_index('ix_case_timeline_case_id', table_name='case_timeline')
    op.drop_index('ix_case_timeline_id', table_name='case_timeline')
    op.drop_table('case_timeline')
