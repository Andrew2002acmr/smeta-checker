"""Alembic environment: URL from DATABASE_URL, metadata from storage_models."""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage_models import Base

config = context.config
if config.config_file_name is not None:
    # Never disable app loggers: migrate() runs inside audit processes/tests.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _db_url():
    override = config.get_main_option("db.url_override")
    return override or os.environ.get(
        "DATABASE_URL", config.get_main_option("sqlalchemy.url")
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = {
        key: value
        for key, value in config.get_section(config.config_ini_section, {}).items()
        if not key.startswith("db.")
    }
    section["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
