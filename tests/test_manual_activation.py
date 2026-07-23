"""Backfill / arbitrary-trade-date runs (wayfinder ticket 13).

The wake loop only ever drives resolve_active_date(now)'s rows — so before
this feature, retrying a PAST day's FAILED segment silently did nothing after
rollover. Now: retry / POST /edp/run set manually_activated; the loop's
manual sweep (_process_manually_activated) drives those rows for any date
within the lookback, with window gating bypassed; any terminal transition
clears the marker.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from src.agent.edp import repository
from src.agent.edp.models import SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient

from . import helpers


def _orchestrator(cfg):
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    return EdpOrchestrator(cfg, cbos)


async def _fail_segment(session_factory, trade_date, segment_code):
    async with session_factory() as session:
        row = await repository.get_one(session, trade_date, segment_code)
        await repository.move_to_state(
            session, row, SegmentStatus.FAILED, category="CBOS_ERROR", reason="forced by test",
        )
        return row


async def _sweep_until_terminal(orchestrator, session_factory, active_date, past_date,
                                segment_code, max_cycles=60):
    """Wake cycles on a LATER active date: only the manual sweep may touch
    past_date's rows."""
    orchestrator._cycle_active_date = active_date
    for _ in range(max_cycles):
        orchestrator._cycle_now = datetime.now(orchestrator._tz)
        summary: dict = {}
        await orchestrator._process_manually_activated(summary)
        async with session_factory() as session:
            row = await repository.get_one(session, past_date, segment_code)
        if row.segment_status in helpers.TERMINAL_STATES:
            return row, summary
    raise TimeoutError(f"{segment_code} {past_date} never reached terminal via manual sweep")


async def test_past_date_retry_actually_runs(cfg, session_factory, test_date):
    """The bug this ticket exists for: FAILED on day D, retried after
    rollover — the sweep must drive it to COMPLETED and clear the marker."""
    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    await _fail_segment(session_factory, test_date, "MCX")

    async with session_factory() as session:
        row = await repository.retry_segment(session, test_date, "MCX")
        await session.commit()
    assert row is not None and row.manually_activated is True
    assert row.segment_status == SegmentStatus.PENDING

    next_day = test_date + timedelta(days=1)
    row, summary = await _sweep_until_terminal(
        orchestrator, session_factory, next_day, test_date, "MCX",
    )
    assert row.segment_status == SegmentStatus.COMPLETED
    assert row.manually_activated is False, "terminal transition must clear the marker"
    assert summary.get("manual_runs_processed", 0) >= 1


async def test_sweep_excludes_active_date_and_bounds_lookback(cfg, session_factory, test_date):
    await helpers.seed_day(session_factory, test_date, cfg)
    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "MCX")
        row.manually_activated = True
        await session.commit()

    async with session_factory() as session:
        # Active date == the row's date -> excluded (normal path owns it).
        same_day = await repository.get_manually_activated_rows(
            session, exclude_date=test_date, min_date=test_date - timedelta(days=30),
        )
        assert all(r.trade_date != test_date for r in same_day)

        # Later active date, row within lookback -> included.
        included = await repository.get_manually_activated_rows(
            session, exclude_date=test_date + timedelta(days=1),
            min_date=test_date - timedelta(days=30),
        )
        assert any(r.trade_date == test_date and r.segment_code == "MCX" for r in included)

        # Row older than the lookback bound -> excluded.
        excluded = await repository.get_manually_activated_rows(
            session, exclude_date=test_date + timedelta(days=40),
            min_date=test_date + timedelta(days=10),
        )
        assert not any(r.trade_date == test_date for r in excluded)


async def test_activate_segment_run_semantics(cfg, session_factory, test_date):
    await helpers.seed_day(session_factory, test_date, cfg)

    async with session_factory() as session:
        wf = await repository.get_active(session, test_date)

        # PENDING row -> activated.
        outcome, row = await repository.activate_segment_run(session, wf, test_date, "MCX")
        assert outcome == "activated" and row.manually_activated is True

        # COMPLETED -> refused (re-running finished billing is not one API
        # call away); the terminal transition also cleared the marker.
        await repository.move_to_state(session, row, SegmentStatus.COMPLETED)
        assert row.manually_activated is False
        outcome, row = await repository.activate_segment_run(session, wf, test_date, "MCX")
        assert outcome == "completed"
        assert row.manually_activated is False

        # IN_PROGRESS -> already_running, marker (re)set so rollover can't
        # orphan it.
        cur = await repository.get_one(session, test_date, "EQ")
        cur.segment_status = SegmentStatus.IN_PROGRESS
        await session.flush()
        outcome, cur = await repository.activate_segment_run(session, wf, test_date, "EQ")
        assert outcome == "already_running" and cur.manually_activated is True
        await session.commit()


@pytest.fixture
def api_client():
    from src.agent.edp.api.control import router as control_router

    app = FastAPI()
    app.include_router(control_router, prefix="/edp")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


ADMIN = {"X-User-Role": "System Administrator"}


async def test_run_endpoint(cfg, session_factory, test_date, api_client):
    await helpers.seed_day(session_factory, test_date, cfg)
    body = {"trade_date": test_date.isoformat(), "segment_code": "mcx"}

    async with api_client as client:
        # Admin-gated: no role -> 403.
        r = await client.post("/edp/run", json=body)
        assert r.status_code == 403

        # Unknown segment -> 404.
        r = await client.post("/edp/run", json={**body, "segment_code": "BOGUS"}, headers=ADMIN)
        assert r.status_code == 404

        # Happy path (code case-normalized) -> 202 + marker set.
        r = await client.post("/edp/run", json=body, headers=ADMIN)
        assert r.status_code == 202, r.text
        assert r.json()["outcome"] == "activated"
        assert r.json()["segment_code"] == "MCX"

        # COMPLETED -> 409.
        async with session_factory() as session:
            row = await repository.get_one(session, test_date, "MCX")
            await repository.move_to_state(session, row, SegmentStatus.COMPLETED)
            await session.commit()
        r = await client.post("/edp/run", json=body, headers=ADMIN)
        assert r.status_code == 409

    async with session_factory() as session:
        eq = await repository.get_one(session, test_date, "EQ")
        assert eq is None or eq.manually_activated is False, "only MCX was activated"
