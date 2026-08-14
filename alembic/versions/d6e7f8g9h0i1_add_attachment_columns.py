"""add file_name + byte_size + mime_type to case_attachments

Revision ID: d6e7f8g9h0i1
Revises: c5d6e7f8g9h0
Create Date: 2026-08-05 20:00:00.000000

Extends the existing `case_attachments` table with three columns that
the upload endpoint needs to populate so the frontend can render the
attachment list without a separate lookup:

  - `file_name VARCHAR(255) NOT NULL DEFAULT ''` — the original
    filename the user uploaded.
  - `byte_size INTEGER NOT NULL DEFAULT 0` — the byte length of the
    decoded binary (the `media_url` column stores the binary as a
    base64 data URL, ~33% larger).
  - `mime_type VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream'`
    — the wire MIME header from the upload (image/jpeg, application/pdf,
    video/mp4, audio/ogg, ...).

All three have `DEFAULT` values so existing rows (created before this
migration by ad-hoc tooling) continue to be valid. The column order is
grouped together at the bottom of the table for clarity.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd6e7f8g9h0i1'
down_revision = 'c5d6e7f8g9h0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'case_attachments',
        sa.Column(
            'file_name',
            sa.String(length=255),
            nullable=False,
            server_default='',
        ),
    )
    op.add_column(
        'case_attachments',
        sa.Column(
            'mime_type',
            sa.String(length=128),
            nullable=False,
            server_default='application/octet-stream',
        ),
    )
    op.add_column(
        'case_attachments',
        sa.Column(
            'byte_size',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )


def downgrade():
    op.drop_column('case_attachments', 'byte_size')
    op.drop_column('case_attachments', 'mime_type')
    op.drop_column('case_attachments', 'file_name')