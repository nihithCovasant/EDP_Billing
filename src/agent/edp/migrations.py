"""
Apply Alembic migrations for the EDP agent schema.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from cams_otel_lib import Logger as logger, otel_trace

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


@otel_trace
def run_migrations() -> None:
    """
    Upgrade to the latest Alembic revision (head).

    Called synchronously during init_database() before the async engine starts.
    Database URL is resolved inside alembic/env.py from the same .env / config
    as the running agent.
    """
    if not _ALEMBIC_INI.is_file():
        raise RuntimeError(f"alembic.ini not found at {_ALEMBIC_INI}")

    cfg = Config(str(_ALEMBIC_INI))
    logger.info("Running Alembic migrations (upgrade head)...")
    command.upgrade(cfg, "head")
    logger.info("Alembic migrations complete")
