"""add social provider fields to users

Revision ID: fff5679580a4
Revises: c23905c51ae5
Create Date: 2025-09-25 11:17:51.644906

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'fff5679580a4'
down_revision = 'c23905c51ae5'
branch_labels = None
depends_on = None

def upgrade():
    # Add social provider fields to users table
    op.add_column('users', sa.Column('google_id', sa.String(), nullable=True))
    op.add_column('users', sa.Column('linkedin_id', sa.String(), nullable=True))
    op.create_unique_constraint('users_google_id_key', 'users', ['google_id'])
    op.create_unique_constraint('users_linkedin_id_key', 'users', ['linkedin_id'])

def downgrade():
    # Remove social provider fields
    op.drop_constraint('users_google_id_key', 'users', type_='unique')
    op.drop_constraint('users_linkedin_id_key', 'users', type_='unique')
    op.drop_column('users', 'linkedin_id')
    op.drop_column('users', 'google_id')