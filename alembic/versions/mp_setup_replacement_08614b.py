"""Create mps table (replaces deleted 08614b68d7ca_added_mp_table).

Revision ID: mp_setup_replacement
Revises: 54cafe7b8adb
Create Date: 2026-08-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mp_setup_replacement'
down_revision: Union[str, Sequence[str], None] = '54cafe7b8adb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Recreate the mps table that the deleted chat-era migration
    # `08614b68d7ca_added_mp_table_and_mp_relationship_in_.py` originally
    # created. The mps table is required by:
    #   - 4c99ed4bc4ff (alter district_id type)
    #   - 7001dfb297f1 (alter district_id nullable)
    #   - 0cc8a1f61058 (add user_id)
    #   - c55579411eaa (add created_at/updated_at/unique)
    # district_id is INTEGER NOT NULL here, matching what 4c99ed4bc4ff expects
    # before altering it to VARCHAR.
    op.create_table(
        'mps',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('district_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('mps')
