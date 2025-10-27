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


# Migration runners
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
