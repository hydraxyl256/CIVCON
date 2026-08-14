"""create case taxonomy and mp regions

Revision ID: c1a2b3d4e5f6
Revises: b2c3d4e5f6a7
Create Date: 2026-08-05 09:00:00.000000

This is migration 1 of 3 for the case-management domain. It creates
the three independent taxonomy + MP tables:

- case_categories   — civic-issue taxonomy (e.g. Health, Education)
- mp_regions        — geographic regions that an MP represents
- mp_profiles       — the richer MP record (parallel to `mps`, NOT
                      a replacement; chat-era `mps` is untouched)

Stage 2 (c2b3c4d5e6f7) creates `cases` + attachments + responses +
the case_number_seq. Stage 3 (c3c4d5e6f7g8) creates timeline + audit
log + assignments + support + the append-only trigger.

Down(revision) is fully reversible via the matching drop statements.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1a2b3d4e5f6'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    # ------------------------------------------------------------------
    # case_categories
    # ------------------------------------------------------------------
    op.create_table(
        'case_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_case_categories_name'),
    )
    op.create_index('ix_case_categories_id', 'case_categories', ['id'])
    op.create_index('ix_case_categories_name', 'case_categories', ['name'])

    # ------------------------------------------------------------------
    # mp_regions
    # ------------------------------------------------------------------
    op.create_table(
        'mp_regions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('district_id', sa.String(length=80), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_mp_regions_name'),
        sa.UniqueConstraint('code', name='uq_mp_regions_code'),
    )
    op.create_index('ix_mp_regions_id', 'mp_regions', ['id'])
    op.create_index('ix_mp_regions_name', 'mp_regions', ['name'])
    op.create_index('ix_mp_regions_district_id', 'mp_regions', ['district_id'])

    # ------------------------------------------------------------------
    # mp_profiles
    # ------------------------------------------------------------------
    op.create_table(
        'mp_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('region_id', sa.Integer(), nullable=True),
        sa.Column('office', sa.String(length=255), nullable=True),
        sa.Column('photo_url', sa.String(length=2048), nullable=True),
        sa.Column('bio', sa.Text(), nullable=True),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column(
            'is_accepting_cases',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['region_id'], ['mp_regions.id'], ondelete='SET NULL',
        ),
        sa.UniqueConstraint('user_id', name='uq_mp_profiles_user_id'),
    )
    op.create_index('ix_mp_profiles_id', 'mp_profiles', ['id'])
    op.create_index('ix_mp_profiles_user_id', 'mp_profiles', ['user_id'])
    op.create_index('ix_mp_profiles_region_id', 'mp_profiles', ['region_id'])

    # ------------------------------------------------------------------
    # Seed default MP regions (idempotent — INSERT ... ON CONFLICT DO NOTHING)
    # ------------------------------------------------------------------
    # The list is intentionally small and conservative. Admins can add
    # new regions via the future admin MP-regions endpoint. The seeding
    # is wrapped in a guard so re-running this migration is safe.
    DEFAULT_REGIONS = [
        ('Central',       'CENTRAL',  'CENTRAL'),
        ('Eastern',       'EASTERN',  'EASTERN'),
        ('Northern',      'NORTHERN', 'NORTHERN'),
        ('Western',       'WESTERN',  'WESTERN'),
        ('Kampala',       'KAMPALA',  'KAMPALA'),
        ('Wakiso',        'WAKISO',   'WAKISO'),
        ('Mukono',        'MUKONO',   'MUKONO'),
        ('Jinja',         'JINJA',    'JINJA'),
        ('Mbale',         'MBALE',    'MBALE'),
        ('Mbarara',       'MBARARA',  'MBARARA'),
        ('Gulu',          'GULU',     'GULU'),
        ('Lira',          'LIRA',     'LIRA'),
        ('Arua',          'ARUA',     'ARUA'),
    ]
    bind = op.get_bind()
    for name, code, district_id in DEFAULT_REGIONS:
        bind.execute(
            sa.text(
                "INSERT INTO mp_regions (name, code, district_id) "
                "VALUES (:name, :code, :district_id) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"name": name, "code": code, "district_id": district_id},
        )


def downgrade():
    # Drop in reverse FK order.
    op.drop_index('ix_mp_profiles_region_id', table_name='mp_profiles')
    op.drop_index('ix_mp_profiles_user_id', table_name='mp_profiles')
    op.drop_index('ix_mp_profiles_id', table_name='mp_profiles')
    op.drop_table('mp_profiles')

    op.drop_index('ix_mp_regions_district_id', table_name='mp_regions')
    op.drop_index('ix_mp_regions_name', table_name='mp_regions')
    op.drop_index('ix_mp_regions_id', table_name='mp_regions')
    op.drop_table('mp_regions')

    op.drop_index('ix_case_categories_name', table_name='case_categories')
    op.drop_index('ix_case_categories_id', table_name='case_categories')
    op.drop_table('case_categories')
