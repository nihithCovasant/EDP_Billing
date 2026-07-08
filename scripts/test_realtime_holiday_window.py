"""
Real-time test: does the agent correctly (a) pick up a freshly-uploaded
config's window_start, (b) hold a segment BLOCKED until that wall-clock time
arrives, and (c) correctly SKIP it as CBOS_SKIP once mock_cbos's
/mock/scenario/holiday flag is set for it — all against the REAL running
mock_cbos HTTP server (not an in-process fake), on real wall-clock time.

Why not just use today's real trade_date directly (as literally described)?
  1. mid-day config-change protection (see tests/test_workflow_defer_midday.py):
     the /edp/workflow upload endpoint silently defers to trade_date+1 once
     *any* segment for today has left PENDING — and today's 7 segments are
     already all COMPLETED in this environment, so a same-day upload would
     never actually apply to today.
  2. Even once applied, retry_segment() (the only sanctioned way to reset a
     segment) refuses to reset an already-COMPLETED row — by design, so ops
     can't accidentally re-run a finished billing segment.

So this script targets an isolated, far-future trade_date (same pattern as
tests/conftest.py::test_date — never touched by the live 24/7 loop, never
collides with real data) but drives it with a "now" whose CLOCK TIME tracks
real wall-clock time every cycle (only the calendar date differs) — so
window_start="HH:MM" gating is tested against the exact real time you'd
expect, without waiting for an actual trading day or touching completed data.

CbosClient is pointed at the REAL mock_cbos server (cfg.cbos_status_url /
cbos_process_url from .env, use_mock=cfg.cbos_use_mock — i.e. real HTTP),
so a manual `POST /mock/scenario/holiday` call against that same running
server (either from this script or from your own curl/Postman) is exactly
what the pipeline sees.

Usage:
    .venv\\Scripts\\python.exe scripts\\test_realtime_holiday_window.py [HH:MM]

    HH:MM defaults to 6 minutes from now if omitted.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

import src.agent.edp.database as edp_database  # noqa: E402
from src.agent.edp import alerts, repository  # noqa: E402
from src.agent.edp.config import build_default_workflow_json, load_edp_config  # noqa: E402
from src.agent.edp.models import SegmentStatus  # noqa: E402
from src.agent.edp.orchestrator import EdpOrchestrator  # noqa: E402
from src.tools.cbos_client import CbosClient  # noqa: E402

from tests import helpers  # noqa: E402

SEGMENT = "EQ"
POLL_SECONDS = 10
MAX_MINUTES = 12


async def main() -> None:
    cfg = load_edp_config()
    tz = ZoneInfo(cfg.timezone)

    if len(sys.argv) > 1:
        hh, mm = (int(x) for x in sys.argv[1].split(":"))
    else:
        target = datetime.now(tz) + timedelta(minutes=6)
        hh, mm = target.hour, target.minute
    window_start = f"{hh:02d}:{mm:02d}"

    print(f"\n[alert config] {alerts.describe_alert_config()}")
    print(f"[cbos] status_url={cfg.cbos_status_url} process_url={cfg.cbos_process_url} use_mock={cfg.cbos_use_mock}")
    print(f"[test] {SEGMENT} window_start={window_start} (real IST wall-clock)\n")

    # Confirm mock_cbos is actually reachable before we commit to this run.
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            health = await client.get(f"{cfg.cbos_status_url}/mock/health")
            print(f"[mock_cbos] health check: {health.json()}\n")
        except Exception as exc:
            print(f"[mock_cbos] NOT reachable at {cfg.cbos_status_url}: {exc}")
            print("Start it first: .venv\\Scripts\\python.exe -m mock_cbos.main")
            return

    engine = create_async_engine(cfg.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    edp_database._engine = engine
    edp_database._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    trade_date: date = date.today() + timedelta(days=6000)
    print(f"[demo] Using synthetic trade_date={trade_date} (isolated — real 'now' clock-time only)\n")

    try:
        await helpers.cleanup_day(session_factory, trade_date)

        workflow_json = build_default_workflow_json([{
            "segment_code": SEGMENT,
            "login_id": cfg.cbos_login_id,
            "window_start": window_start,
            "window_end": "23:59",
            "window_end_next_day": False,
        }])
        async with session_factory() as session:
            await repository.upload(session, trade_date, workflow_json, uploaded_by="realtime-test")
            await session.commit()
        async with session_factory() as session:
            workflow = await repository.get_active(session, trade_date)
            await repository.seed_from_workflow(session, workflow, trade_date)
            await session.commit()
        print(f"[demo] Uploaded config + seeded {SEGMENT} — waiting for real time {window_start} IST...\n")

        cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=cfg.cbos_use_mock)
        cbos.mock_set_ready_after(1)
        orchestrator = EdpOrchestrator(cfg, cbos)

        deadline = datetime.now(tz) + timedelta(minutes=MAX_MINUTES)
        holiday_confirmed = False
        while datetime.now(tz) < deadline:
            now_ist = datetime.now(tz)
            orchestrator._cycle_active_date = trade_date
            orchestrator._cycle_now = datetime.combine(trade_date, now_ist.time(), tzinfo=tz)

            outcome = await orchestrator._process_one_segment(SEGMENT)

            async with session_factory() as session:
                row = await repository.get_one(session, trade_date, SEGMENT)

            if not holiday_confirmed:
                async with httpx.AsyncClient(timeout=5) as client:
                    state = await client.get(f"{cfg.cbos_status_url}/mock/state")
                    holiday_confirmed = SEGMENT in state.json().get("holiday_segments", [])

            print(
                f"[{now_ist.strftime('%H:%M:%S')} IST] outcome={outcome:9s} "
                f"status={row.segment_status.value:11s} phase={row.current_phase} "
                f"mock_cbos_holiday_flag_set={holiday_confirmed}"
            )

            if row.segment_status in (SegmentStatus.COMPLETED, SegmentStatus.SKIPPED, SegmentStatus.FAILED):
                print(f"\n[demo] {SEGMENT} reached terminal state: {row.segment_status.value} "
                      f"category={row.skip_category} reason={row.skip_reason}")
                if not holiday_confirmed:
                    print(
                        f"[demo] NOTE: mock_cbos never reported {SEGMENT} as a holiday segment — "
                        "if you expected a holiday SKIP, make sure you called "
                        f"POST {cfg.cbos_status_url}/mock/scenario/holiday "
                        f'{{"segment":"{SEGMENT}","enabled":true}} before the window opened.'
                    )
                break

            await asyncio.sleep(POLL_SECONDS)
        else:
            print(f"\n[demo] Timed out after {MAX_MINUTES} minutes without reaching a terminal state.")
    finally:
        await engine.dispose()
        print(f"\n[demo] Test data left under trade_date={trade_date} for inspection "
              f"(GET /edp/status/{trade_date}). Cleanup: helpers.cleanup_day(session_factory, {trade_date!r}).")


if __name__ == "__main__":
    asyncio.run(main())
