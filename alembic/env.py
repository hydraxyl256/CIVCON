import os
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from dotenv import load_dotenv
from app.database import Base
from app.models import *  


# Load environment variables (.env) so Alembic uses your DATABASE_URL
load_dotenv()

config = context.config

# Use DATABASE_URL from environment if available
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Alembic expects a synchronous driver (psycopg2)
    if "+asyncpg" in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("+asyncpg", "+psycopg2")

    config.set_main_option("sqlalchemy.url", DATABASE_URL)


# Logging setup
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# CONCURRENTLY-aware transaction wrapping.
#
# Postgres' CREATE INDEX CONCURRENTLY must run *outside* a wrapping
# BEGIN/COMMIT block. When CIVCON_ALEMBIC_CONCURRENTLY=1 the perf
# migration (`b1c2d3e4f5a6_perf_index_constraints_and_fks.py`) emits
# CREATE INDEX CONCURRENTLY IF NOT EXISTS statements, and we skip
# the alembic-default `with context.begin_transaction():` block so
# each statement commits independently. Setting the env var is the
# team-level signal that the migrations are being applied to a live
# database (Render / prod) where holding an AccessExclusiveLock on
# a table is unacceptable.
#
# Outside of that, we keep the alembic default — every migration
# runs inside one transaction so a half-applied change is rolled
# back as a unit on error.
CONCURRENTLY_ENABLED = os.getenv("CIVCON_ALEMBIC_CONCURRENTLY", "1") == "1"


# Migration runners
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # Detect column-type changes in autogenerate.
        compare_server_default=True,  # Detect default-value changes.
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        if CONCURRENTLY_ENABLED:
            # Online-index-build mode: no BEGIN/COMMIT wrapper so
            # CREATE INDEX CONCURRENTLY can run.
            context.run_migrations()
        else:
            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
