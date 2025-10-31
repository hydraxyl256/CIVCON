"""Add full text search index to articles

Revision ID: 90962d6bfa3f
Revises: a1ab67274b10
Create Date: 2025-10-31 08:47:32.464176
"""

from typing import Sequence, Union
from alembic import op

# Revision identifiers
revision: str = "90962d6bfa3f"
down_revision: Union[str, Sequence[str], None] = "a1ab67274b10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add safe full-text search index."""

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_search_tsv
        ON articles
        USING GIN (
            (
                setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(content, '')), 'C')
            )
        );
        """
    )


def downgrade() -> None:
    """Downgrade schema: drop full-text search index."""
    op.execute("DROP INDEX IF EXISTS idx_articles_search_tsv;")
