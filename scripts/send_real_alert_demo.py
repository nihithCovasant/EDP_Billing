"""
Run a full simulated EDP day through the REAL production pipeline
(orchestrator -> pipeline.stages -> alerts) and let it send a REAL email
via global_email_service / Microsoft Graph when a segment genuinely fails.

Why this is safe to run against the same database the live agent uses:
  - Uses a random, far-future trade_date (see tests/conftest.py::test_date
    for the same pattern) — the live 24/7 wake loop only ever resolves
    *today's* real active_date (utils.datetime_utils.resolve_active_date),
    so it can never see or touch this row. No production data is read or
    written.
  - CBOS itself is never called over HTTP — FailingCbosClient wraps
    CbosClient's own built-in in-process mock (use_mock=True), so
    mock_cbos.main does not even need to be running for this script.

What it does:
  1. Seeds a full day (7 segments, all windows wide open) for a synthetic
     trade_date.
  2. Drives every segment through the real orchestrator/pipeline, with
     EQ's BILLPOSTING step wired to return a permanent (non-transient)
     CBOS error — the exact same scenario as
     tests/test_day2_segment_process_failure.py.
  3. That permanent error makes pipeline.stages._fail() mark EQ FAILED,
     which calls alerts.send_failure_alert() for real — the actual send
     is real Microsoft Graph traffic (EDP_EMAIL_ALERTS_ENABLED=true /
     EMAIL_DRY_RUN=false in .env, unlike the test suite where
     tests/conftest.py's autouse fixture forces alerts off).

Usage:
    .venv\\Scripts\\python.exe scripts\\send_real_alert_demo.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

import src.agent.edp.database as edp_database  # noqa: E402
from src.agent.edp import alerts  # noqa: E402
from src.agent.edp.config import load_edp_config  # noqa: E402
from src.agent.edp.orchestrator import EdpOrchestrator  # noqa: E402

from tests import helpers  # noqa: E402
from tests.fakes import FailingCbosClient  # noqa: E402

FAIL_SEGMENT = "EQ"
FAIL_PROCESS = "BILLPOSTING"


async def main() -> None:
    cfg = load_edp_config()

    print(f"\n[alert config] {alerts.describe_alert_config()}\n")
    if not alerts.alerts_enabled():
        print("EDP_EMAIL_ALERTS_ENABLED is false — no email will be sent. Aborting.")
        return

    engine = create_async_engine(cfg.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    edp_database._engine = engine
    edp_database._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    offset_days = 5000 + (uuid.uuid4().int % 50000)
    trade_date = date.today() + timedelta(days=offset_days)
    print(f"[demo] Using synthetic trade_date={trade_date} (isolated from real data)\n")

    try:
        await helpers.cleanup_day(session_factory, trade_date)
        await helpers.seed_day(session_factory, trade_date, cfg)

        cbos = FailingCbosClient(
            cfg.cbos_status_url, cfg.cbos_process_url,
            fail_segment=FAIL_SEGMENT, fail_process=FAIL_PROCESS,
        )
        cbos.mock_set_ready_after(1)
        orchestrator = EdpOrchestrator(cfg, cbos)

        print(f"[demo] Driving the day — {FAIL_SEGMENT}/{FAIL_PROCESS} will fail permanently...\n")
        rows = await helpers.drive_until_terminal(orchestrator, session_factory, trade_date)

        print("\n[demo] Final segment statuses:")
        for row in rows:
            print(f"  {row.segment_code:8s} {row.segment_status.value:10s} "
                  f"category={row.skip_category} reason={row.skip_reason}")

        failing_row = next(r for r in rows if r.segment_code == FAIL_SEGMENT)
        if failing_row.segment_status.value == "FAILED":
            print(
                f"\n[demo] {FAIL_SEGMENT} FAILED as expected — a real FAILED alert email "
                f"should have been sent/attempted above via _fail() -> alerts.send_failure_alert()."
            )
        else:
            print(f"\n[demo] Unexpected: {FAIL_SEGMENT} ended {failing_row.segment_status.value}")
    finally:
        await engine.dispose()
        print(f"\n[demo] Test data left in place under trade_date={trade_date} for inspection "
              f"(GET /edp/status/{trade_date}). Safe to leave — never touched by the live wake loop.")


if __name__ == "__main__":
    asyncio.run(main())
