from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f648a61c5f30'
down_revision = '9843c99cfa40'
branch_labels = None
depends_on = None

def upgrade():
    # 1️⃣ Create comments table with media support
    op.create_table(
        'comments',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id')),
        sa.Column('post_id', sa.Integer, sa.ForeignKey('posts.id')),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('media_url', sa.String, nullable=True),  # new column for media
        sa.Column('parent_id', sa.Integer, sa.ForeignKey('comments.id'), nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now())
    )

    # 2️⃣ Create enum type if it doesn't exist
    role_enum = postgresql.ENUM('CITIZEN', 'MP', 'JOURNALIST', 'ADMIN', name='role')
    role_enum.create(op.get_bind(), checkfirst=True)

    # 3️⃣ Update existing roles safely
    op.execute("""
        UPDATE users
        SET role = CASE
            WHEN LOWER(role) = 'citizen' THEN 'CITIZEN'
            WHEN LOWER(role) = 'mp' THEN 'MP'
            WHEN LOWER(role) = 'journalist' THEN 'JOURNALIST'
            WHEN LOWER(role) = 'admin' THEN 'ADMIN'
            ELSE NULL
        END
        WHERE role IS NOT NULL;
    """)

def downgrade():
    op.drop_table('comments')
    # optional: op.execute('DROP TYPE IF EXISTS role;')
