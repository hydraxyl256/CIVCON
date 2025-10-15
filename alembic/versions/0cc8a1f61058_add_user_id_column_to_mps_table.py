"""Add user_id column to mps table

Revision ID: 0cc8a1f61058
Revises: 7001dfb297f1
Create Date: 2025-10-15 09:23:23.892309

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0cc8a1f61058'
down_revision: Union[str, Sequence[str], None] = '7001dfb297f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # Add the user_id column
    op.add_column('mps', sa.Column('user_id', sa.Integer(), nullable=True))

    # Create the foreign key constraint linking mps.user_id → users.id
    op.create_foreign_key(
        'fk_mps_user_id_users',
        source_table='mps',
        referent_table='users',
        local_cols=['user_id'],
        remote_cols=['id'],
        ondelete='CASCADE'
    )

    pass


def downgrade() -> None:

    """Downgrade schema."""

    # Drop the foreign key first
    op.drop_constraint('fk_mps_user_id_users', 'mps', type_='foreignkey')

    # Then drop the user_id column
    op.drop_column('mps', 'user_id')
    pass
