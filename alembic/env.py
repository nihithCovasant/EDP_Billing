"""
Alembic migration environment for EDP Billing agent tables.

Uses the same DATABASE_URL / DB_* resolution as the running agent (see
src/agent/edp/config.py). Migrations run on a sync driver (psycopg / sqlite);
the app runtime continues to use asyncpg / aiosqlite.
"""

from __future__ import annotations

import logging
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Repo root on sys.path so `src.*` imports work when invoked from project root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from src.agent.edp.config import load_edp_config, to_alembic_url  # noqa: E402
from src.agent.edp.models import Base  # noqa: E402, F401 — registers all models

config = context.config

# alembic.ini's [logger_root] section installs a raw, direct StreamHandler
# onto the *root* logger via fileConfig() — fine for a standalone
# `alembic upgrade head` CLI run, but destructive when migrations run
# in-process inside the EDP agent: run_migrations() executes on a worker
# thread via asyncio.to_thread during agent startup, and fileConfig()'s
# handler swap happens live, tearing out the agent's queue-based
# non-blocking log handler (see src/agent/__main__.py) and replacing it with
# a direct sys.stderr write. From that point on, *every* subsequent log call
# in *any* thread (including the main event loop) blocks the instant that
# stream's pipe/console isn't being actively drained — exactly the
# "logs stop appearing right after migrations" symptom this caused.
# Heuristic: only let fileConfig configure logging when nothing already has
# (i.e. we're a standalone `alembic` CLI invocation) — when the root logger
# already has handlers, a host process (the agent) owns logging and we must
# not clobber it.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return to_alembic_url(load_edp_config().database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL script generation)."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
