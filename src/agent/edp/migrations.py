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
    Upgrade to the latest Alembic revision (head). Called synchronously
    during init_database(), off the event loop via asyncio.to_thread.

    When already at head, Alembic logs nothing but two boilerplate lines —
    the before/after logs here (with elapsed_ms) make that silence
    unambiguous rather than looking like a hang.
    """
    if not _ALEMBIC_INI.is_file():
        raise RuntimeError(f"alembic.ini not found at {_ALEMBIC_INI}")

    cfg = Config(str(_ALEMBIC_INI))
    logger.info("Running Alembic migrations (upgrade head)...")
    t0 = time.monotonic()
    command.upgrade(cfg, "head")
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(f"Alembic migrations complete (elapsed_ms={elapsed_ms}, already at head if this was fast)")
