"""add post_media table

Revision ID: 10a2139608f8
Revises: f648a61c5f30
Create Date: 2025-09-26 16:41:23.477488

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '10a2139608f8'
down_revision: Union[str, Sequence[str], None] = 'f648a61c5f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'post_media',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('post_id', sa.Integer(), sa.ForeignKey('posts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('file_url', sa.String(), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.func.now())
    )



    pass


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('post_media')
    pass
