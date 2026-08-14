"""add auth_sessions table and users.last_login_at

Revision ID: a1b2c3d4e5f6
Revises: fff5679580a4
Create Date: 2026-07-14 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'fff5679580a4'
branch_labels = None
depends_on = None


def upgrade():
    # Add last_login_at to users (nullable — existing rows have no recorded login)
    op.add_column(
        'users',
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    )

    # New auth_sessions table
    op.create_table(
        'auth_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('family_id', sa.String(length=64), nullable=False),
        sa.Column('current_jti', sa.String(length=64), nullable=False),
        sa.Column('user_agent', sa.String(), nullable=True),
        sa.Column('ip_address', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('revoked_reason', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_auth_sessions_id', 'auth_sessions', ['id'])
    op.create_index('ix_auth_sessions_user_id', 'auth_sessions', ['user_id'])
    op.create_index('ix_auth_sessions_family_id', 'auth_sessions', ['family_id'], unique=True)


def downgrade():
    op.drop_index('ix_auth_sessions_family_id', table_name='auth_sessions')
    op.drop_index('ix_auth_sessions_user_id', table_name='auth_sessions')
    op.drop_index('ix_auth_sessions_id', table_name='auth_sessions')
    op.drop_table('auth_sessions')
    op.drop_column('users', 'last_login_at')
