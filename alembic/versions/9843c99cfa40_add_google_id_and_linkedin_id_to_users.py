"""add google_id and linkedin_id to users

Revision ID: 9843c99cfa40
Revises: 6e11fd2802d0
Create Date: 2025-09-25 17:16:33.532172

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9843c99cfa40'
down_revision: Union[str, Sequence[str], None] = '6e11fd2802d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("google_id", sa.String(), nullable=True))
    op.add_column("users", sa.Column("linkedin_id", sa.String(), nullable=True))
    pass


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_column("users", "google_id")
    op.drop_column("users", "linkedin_id")
    pass
