"""
Shared pytest fixtures for the EDP Billing test suite.

These are integration tests that run against the SAME database the agent
itself uses (see src/agent/edp/config.py::load_edp_config — DATABASE_URL /
DB_* env vars, same as the running agent / mock_cbos setup documented in
the project README). Nothing here is mocked at the database layer.

Isolation strategy: every test gets a unique, far-future trade_date (see
`test_date` fixture) so it can never collide with real trading data, and
can never be touched by a live agent instance's wake loop — that loop only
ever resolves *today's* real active_date (see utils/datetime_utils.
resolve_active_date), never a date thousands of days out.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

import src.agent.edp.database as edp_database
from src.agent.edp import edpb_client as edpb_client_module
from src.agent.edp.config import EdpBootstrapConfig, load_edp_config
from src.agent.edp.edpb_client import EdpbClient

from . import helpers


@pytest.fixture(autouse=True)
def edpb_mock_client():
    """Every test gets a deterministic in-process EdpbClient mock (mirrors how
    tests construct CbosClient(use_mock=True) explicitly): downloads succeed
    with a canned manifest, batch submission is accepted, batch status is
    confirmed. Tests exercising failure paths swap in their own via
    set_edpb_client()."""
    edpb_client_module.set_edpb_client(EdpbClient("http://edpb-bot.mock", "http://edpb-uploader.mock", use_mock=True))
    yield
    edpb_client_module.reset_edpb_client()


@pytest.fixture(scope="session")
def cfg() -> EdpBootstrapConfig:
    return load_edp_config()


@pytest_asyncio.fixture
async def engine(cfg: EdpBootstrapConfig):
    """
    Function-scoped (not session-scoped) on purpose: pytest-asyncio's
    default "auto" mode gives each async test its own event loop, and an
    asyncpg-backed async engine's connection pool is bound to the loop it
    was created in. Recreating + disposing the engine per test keeps it
    tied to that test's own loop and avoids cross-loop connection errors.
    """
    eng = create_async_engine(cfg.database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def wire_orchestrator_database(engine):
    """
    orchestrator._process_one_segment() (and everything else in
    repository/*) doesn't take a session — it opens its own via the
    module-level src.agent.edp.database.get_session(), which requires
    init_database() to have set the module's private _engine/_session_factory
    globals first.

    We deliberately do NOT call init_database() here: it also runs the
    Alembic migration chain (run_migrations()), which is unrelated to what
    these tests exercise and has been flaky to hang in this environment.
    Instead we point the module's globals directly at this test's own
    engine (same DB, already migrated) so get_session() works, and restore
    the previous globals afterwards so this can't leak into other tests
    (e.g. ones that DO call init_database()).
    """
    previous_engine = edp_database._engine
    previous_factory = edp_database._session_factory
    edp_database._engine = engine
    edp_database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield
    finally:
        edp_database._engine = previous_engine
        edp_database._session_factory = previous_factory


@pytest.fixture(autouse=True)
def no_real_emails(monkeypatch):
    """
    Prevent tests from ever sending real alert emails, regardless of what's
    configured in the developer's .env (EMAIL_DRY_RUN/EMAIL_GRAPH_*).
    """
    import global_email_service

    monkeypatch.setattr(global_email_service, "send_segment_alert", lambda payload: None)


@pytest_asyncio.fixture
async def test_date(session_factory):
    """
    A unique, far-future trade_date for this test. Cleans up any
    edp_properties/segment_execution rows for that date both before (in
    case a previous crashed run left something behind) and after the test.
    """
    offset_days = 5000 + (uuid.uuid4().int % 50000)
    trade_date = date.today() + timedelta(days=offset_days)

    await helpers.cleanup_day(session_factory, trade_date)
    yield trade_date
    await helpers.cleanup_day(session_factory, trade_date)
