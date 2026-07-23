"""
Async database engine and session factory for the EDP agent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .migrations import run_migrations

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


@otel_trace
async def init_database(database_url: str) -> None:
    global _engine, _session_factory
    # Alembic upgrade is synchronous — run off the event loop so a fresh-DB
    # migration pass (can take tens of seconds on PostgreSQL) doesn't freeze
    # the entire HTTP server and make startup look hung after the last
    # "Running upgrade ..." line with no "Wake loop started" yet.
    await asyncio.to_thread(run_migrations)
    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    logger.info(f"EDP database initialized: {database_url.split('://')[0]}://...")


@otel_trace
async def close_database() -> None:
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@otel_trace
async def check_connectivity() -> dict[str, Any]:
    """
    Live "SELECT 1" against the EDP database — used by GET /edp/health (see
    src/agent/__main__.py). Deliberately a fresh live check every call rather
    than a passively-cached timestamp: a stale "last successful" time would
    hide an outage that started right after the last successful check.
    """
    if _session_factory is None:
        return {"status": "error", "error": "Database not initialized", "latency_ms": 0}
    t0 = time.monotonic()
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "latency_ms": int((time.monotonic() - t0) * 1000)}
    except Exception as exc:
        logger.error(f"EDP database connectivity check failed: {exc}")
        return {"status": "error", "error": str(exc), "latency_ms": int((time.monotonic() - t0) * 1000)}


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
