"""add extended user fields

Revision ID: c23905c51ae5
Revises: 
Create Date: 2025-09-24 15:17:33.279446

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c23905c51ae5'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    """Upgrade schema."""
    # Add first_name and last_name as nullable to avoid NotNullViolation
    op.add_column('users', sa.Column('first_name', sa.String(), nullable=True))
    op.add_column('users', sa.Column('last_name', sa.String(), nullable=True))

    # Populate first_name and last_name based on username
    op.execute("""
        UPDATE users 
        SET first_name = CASE 
            WHEN strpos(username, ' ') > 0 THEN split_part(username, ' ', 1)
            WHEN strpos(username, '_') > 0 THEN split_part(username, '_', 1)
            ELSE username
        END,
        last_name = CASE 
            WHEN strpos(username, ' ') > 0 THEN split_part(username, ' ', 2)
            WHEN strpos(username, '_') > 0 THEN split_part(username, '_', 2)
            ELSE 'Unknown'
        END
        WHERE first_name IS NULL
    """)

    # Make first_name and last_name non-nullable
    op.alter_column('users', 'first_name', nullable=False)
    op.alter_column('users', 'last_name', nullable=False)

    # Add remaining columns
    op.add_column('users', sa.Column('region', sa.String(), nullable=True))
    op.add_column('users', sa.Column('district_id', sa.String(), nullable=True))
    op.add_column('users', sa.Column('county_id', sa.String(), nullable=True))
    op.add_column('users', sa.Column('sub_county_id', sa.String(), nullable=True))
    op.add_column('users', sa.Column('parish_id', sa.String(), nullable=True))
    op.add_column('users', sa.Column('village_id', sa.String(), nullable=True))
    op.add_column('users', sa.Column('occupation', sa.String(), nullable=True))
    op.add_column('users', sa.Column('bio', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('profile_image', sa.String(), nullable=True))
    op.add_column('users', sa.Column('political_interest', sa.String(), nullable=True))
    op.add_column('users', sa.Column('community_role', sa.String(), nullable=True))
    op.add_column('users', sa.Column('interests', sa.JSON(), nullable=True))
    op.add_column('users', sa.Column('notifications', sa.JSON(), nullable=True))
    op.add_column('users', sa.Column('privacy_level', sa.String(), nullable=True, server_default='public'))

    # Drop username index and column
    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_column('users', 'username')

def downgrade() -> None:
    """Downgrade schema."""
    # Add username back as nullable first
    op.add_column('users', sa.Column('username', sa.String(), nullable=True))

    # Populate username from first_name and last_name
    op.execute("UPDATE users SET username = first_name || '_' || last_name")

    # Make username non-nullable and recreate index
    op.alter_column('users', 'username', nullable=False)
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)

    # Drop added columns
    op.drop_column('users', 'privacy_level')
    op.drop_column('users', 'notifications')
    op.drop_column('users', 'interests')
    op.drop_column('users', 'community_role')
    op.drop_column('users', 'political_interest')
    op.drop_column('users', 'profile_image')
    op.drop_column('users', 'bio')
    op.drop_column('users', 'occupation')
    op.drop_column('users', 'village_id')
    op.drop_column('users', 'parish_id')
    op.drop_column('users', 'sub_county_id')
    op.drop_column('users', 'county_id')
    op.drop_column('users', 'district_id')
    op.drop_column('users', 'region')
    op.drop_column('users', 'last_name')
    op.drop_column('users', 'first_name')