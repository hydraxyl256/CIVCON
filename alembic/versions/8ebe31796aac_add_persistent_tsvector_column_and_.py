"""Add persistent tsvector column and trigger for articles full-text search

Revision ID: 8ebe31796aac
Revises: 90962d6bfa3f
Create Date: 2025-10-31 09:18:57.998422

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8ebe31796aac'
down_revision: Union[str, Sequence[str], None] = '90962d6bfa3f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

     #  Add the tsvector column
    op.add_column("articles", sa.Column("tsv_document", sa.types.TEXT))

    #  Populate the new column initially
    op.execute(
        """
        UPDATE articles
        SET tsv_document = 
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(content, '')), 'C');
        """
    )

    #  Create a GIN index for fast search
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_tsv_gin
        ON articles
        USING GIN (to_tsvector('english', tsv_document));
        """
    )

    #  Create the trigger to keep it updated automatically
    op.execute(
        """
        CREATE FUNCTION articles_tsvector_trigger() RETURNS trigger AS $$
        begin
            new.tsv_document :=
                setweight(to_tsvector('english', coalesce(new.title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(new.summary, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(new.content, '')), 'C');
            return new;
        end
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER tsvectorupdate
        BEFORE INSERT OR UPDATE ON articles
        FOR EACH ROW
        EXECUTE PROCEDURE articles_tsvector_trigger();
        """
    )

    pass


def downgrade() -> None:
    """Downgrade schema."""

    op.execute("DROP TRIGGER IF EXISTS tsvectorupdate ON articles;")
    op.execute("DROP FUNCTION IF EXISTS articles_tsvector_trigger;")
    op.execute("DROP INDEX IF EXISTS idx_articles_tsv_gin;")
    op.drop_column("articles", "tsv_document")
    pass
