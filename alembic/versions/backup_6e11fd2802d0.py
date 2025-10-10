from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = '6e11fd2802d0'
down_revision = 'fff5679580a4'

def upgrade():
    # Ensure tsvector extension
    # op.execute('CREATE EXTENSION IF NOT EXISTS tsvector')

    # Create posts table
    op.create_table(
        'posts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('author_id', sa.Integer(), nullable=False),
        sa.Column('district_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.Column('search_vector', TSVECTOR, nullable=True),
        sa.ForeignKeyConstraint(['author_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )

    # Create role enum
    op.execute("CREATE TYPE role AS ENUM ('citizen', 'mp', 'journalist', 'admin')")

    # Create votes table
    op.create_table(
        'votes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('vote_type', sa.String(), nullable=False, server_default='like'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.PrimaryKeyConstraint('id')
    )

    # Create messages table
    op.create_table(
        'messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('sender_id', sa.Integer(), nullable=False),
        sa.Column('recipient_id', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('district_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['sender_id'], ['users.id']),
        sa.ForeignKeyConstraint(['recipient_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    

    # Create live_feeds table
    op.create_table(
        'live_feeds',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=True),
        sa.Column('journalist_id', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('district_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['journalist_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )

    # ✅ Create post_media table for optional media
    op.create_table(
        'post_media',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('post_id', sa.Integer(), sa.ForeignKey('posts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('file_url', sa.String(), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.func.now())
    )


def downgrade():
    op.drop_table('post_media')
    op.drop_table('live_feeds')
    op.drop_table('messages')
    op.drop_table('votes')
    op.drop_column('users', 'role')
    op.execute("DROP TYPE role")
    op.drop_table('posts')
