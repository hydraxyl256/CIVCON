"""create admin_settings table with role enum

Revision ID: a5991b1176ce
Revises: f207647559d2
Create Date: 2025-11-03 17:06:28.973408

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5991b1176ce'
down_revision: Union[str, Sequence[str], None] = 'f207647559d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
