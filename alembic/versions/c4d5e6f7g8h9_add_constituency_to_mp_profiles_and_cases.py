"""add nullable constituency column to mp_profiles and cases

Revision ID: c4d5e6f7g8h9
Revises: c3c4d5e6f7g8
Create Date: 2026-08-05 14:00:00.000000

Adds a `constituency` String(120) column to both `mp_profiles` and
`cases`. The column is nullable + indexed so existing rows do not
need a backfill and the MP routing engine (services/cases/routing.py)
can use the column in WHERE/ORDER BY clauses without a table scan.

The column is intentionally separate from:
- `region_id` (the larger administrative region, FK to mp_regions)
- `office` (free-text description of where the MP holds office)
- `district_id` (the free-form district string used everywhere else)

This separation lets routing rank MPs by three independent signals
without the column-overloading that bites schema design later.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7g8h9'
down_revision = 'c3c4d5e6f7g8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'mp_profiles',
        sa.Column('constituency', sa.String(length=120), nullable=True),
    )
    op.create_index(
        'ix_mp_profiles_constituency',
        'mp_profiles',
        ['constituency'],
    )

    op.add_column(
        'cases',
        sa.Column('constituency', sa.String(length=120), nullable=True),
    )
    op.create_index(
        'ix_cases_constituency',
        'cases',
        ['constituency'],
    )


def downgrade():
    op.drop_index('ix_cases_constituency', table_name='cases')
    op.drop_column('cases', 'constituency')

    op.drop_index('ix_mp_profiles_constituency', table_name='mp_profiles')
    op.drop_column('mp_profiles', 'constituency')