"""add ussd_sessions and spam_scores

Revision ID: 930953331187
Revises: 52765d309052
Create Date: 2025-10-03 18:58:01.231053

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '930953331187'
down_revision: Union[str, Sequence[str], None] = '52765d309052'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('ussd_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('phone_number', sa.String(), nullable=False),
        sa.Column('session_id', sa.String(), nullable=True),
        sa.Column('current_step', sa.String(), nullable=False),  # consent, language, register, ask_question, topic_menu, question
        sa.Column('user_data', sa.JSON(), nullable=True),  # Stored form data (name, district, etc.)
        sa.Column('language', sa.String(), default='EN'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('phone_number', 'session_id')
    )
    op.create_index('idx_ussd_sessions_phone', 'ussd_sessions', ['phone_number'], unique=False)

    # Add spam_scores column to messages
    op.add_column('messages', sa.Column('spam_score', sa.Float(), nullable=True))
    op.add_column('messages', sa.Column('is_spam', sa.Boolean(), nullable=True))
    pass
    # ### end Alembic commands ###

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('ussd_sessions')
    op.drop_column('messages', 'is_spam')
    op.drop_column('messages', 'spam_score')
    pass
    # ### end Alembic commands ###
