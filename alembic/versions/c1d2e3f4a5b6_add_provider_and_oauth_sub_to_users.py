"""add provider and oauth_sub columns to users

Revision ID: c1d2e3f4a5b6
Revises: a3b4c5d6e7f8
Create Date: 2026-08-03 12:00:00.000000

The OAuth callbacks (Google, LinkedIn) create new users with
``provider='google'`` / ``provider='linkedin'`` so we know which
identity provider minted the account. The ``oauth_sub`` column records
the provider's ``sub`` claim so future sign-ins can be linked without
silently merging accounts on email alone.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c1d2e3f4a5b6"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the provider + oauth_sub columns to users."""
    op.add_column(
        "users",
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=True,
            server_default="",
        ),
    )
    op.add_column(
        "users",
        sa.Column("oauth_sub", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Drop the provider + oauth_sub columns."""
    op.drop_column("users", "oauth_sub")
    op.drop_column("users", "provider")