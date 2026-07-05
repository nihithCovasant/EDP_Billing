"""
Apply Alembic migrations for the EDP agent schema.
"""

from __future__ import annotations

import time
from pathlib import Path

from alembic import command
from alembic.config import Config

from cams_otel_lib import Logger as logger, otel_trace

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


@otel_trace
def run_migrations() -> None:
    """
    Upgrade to the latest Alembic revision (head).

    Called synchronously during init_database() before the async engine starts
    (off the event loop, via asyncio.to_thread — see database.py).
    Database URL is resolved inside alembic/env.py from the same .env / config
    as the running agent.

    Note: when the DB is already at head (the common case on every restart
    after the first), Alembic itself only logs two "Context impl.../Will
    assume transactional DDL" lines and nothing else — it does NOT print a
    "no migrations to run" line. That silence is normal, not a hang; the
    explicit before/after logs here (with elapsed_ms) make that unambiguous
    so a quiet gap here isn't mistaken for the process being stuck.
    """
    if not _ALEMBIC_INI.is_file():
        raise RuntimeError(f"alembic.ini not found at {_ALEMBIC_INI}")

    cfg = Config(str(_ALEMBIC_INI))
    logger.info("Running Alembic migrations (upgrade head)...")
    t0 = time.monotonic()
    command.upgrade(cfg, "head")
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(f"Alembic migrations complete (elapsed_ms={elapsed_ms}, already at head if this was fast)")
