"""Alembic environment.

Reads DATABASE_URL from the environment. Falls back to alembic.ini's
sqlalchemy.url (which is intentionally blank). If neither is set, online
migrations will fail clearly — but local development does not require
Alembic to run, so this only affects users who explicitly opt into Postgres.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    env_url = os.getenv("DATABASE_URL")
    if env_url and env_url.strip():
        return env_url.strip()
    ini_url = config.get_main_option("sqlalchemy.url") or ""
    return ini_url.strip()


target_metadata = None


def run_migrations_offline() -> None:
    url = _resolve_url()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it before running Alembic migrations."
        )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it before running Alembic migrations."
        )
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
