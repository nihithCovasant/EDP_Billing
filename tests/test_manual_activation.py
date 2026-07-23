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
            session,
            row,
            SegmentStatus.FAILED,
            category="CBOS_ERROR",
            reason="forced by test",
        )
        return row


async def _sweep_until_terminal(orchestrator, session_factory, active_date, past_date, segment_code, max_cycles=60):
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
        orchestrator,
        session_factory,
        next_day,
        test_date,
        "MCX",
    )
    assert row.segment_status == SegmentStatus.COMPLETED
    assert row.manually_activated is False, "terminal transition must clear the marker"
    assert summary.get("manual_runs_processed", 0) >= 1


async def test_sweep_ownership_and_lookback(cfg, session_factory, test_date):
    """The repository returns every marked row in the lookback; the SWEEP
    decides ownership: active-date rows whose segment today's config drives
    are skipped (normal path owns them), but an active-date row for a segment
    MISSING from today's config belongs to the sweep — review finding: the
    old date-based exclusion orphaned exactly that row until rollover."""
    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "MCX")
        row.manually_activated = True
        await session.commit()

    async with session_factory() as session:
        # Lookback bound still applies at the repository.
        included = await repository.get_manually_activated_rows(
            session,
            min_date=test_date - timedelta(days=30),
        )
        assert any(r.trade_date == test_date and r.segment_code == "MCX" for r in included)
        excluded = await repository.get_manually_activated_rows(
            session,
            min_date=test_date + timedelta(days=10),
        )
        assert not any(r.trade_date == test_date for r in excluded)

    # Sweep on the SAME active date: MCX is in today's configured codes ->
    # the sweep must not double-drive it.
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = datetime.now(orchestrator._tz)
    orchestrator._cycle_configured_codes = ("EQ", "MCX")
    summary: dict = {}
    await orchestrator._process_manually_activated(summary)
    assert summary.get("manual_runs_processed", 0) == 0

    # Same active date, but MCX absent from today's config -> the sweep owns
    # it (the row would otherwise be orphaned until rollover).
    orchestrator._cycle_configured_codes = ("EQ",)
    summary = {}
    await orchestrator._process_manually_activated(summary)
    assert summary.get("manual_runs_processed", 0) >= 1


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


async def test_post_trade_rows_never_enter_the_manual_lane(cfg, session_factory, test_date):
    """Round-2 review: retry_segment marked post-trade rows too, and the sweep
    then drove them through the real-segment machine into a misleading error
    every cycle. Post-trade rows are now excluded at both the marker and the
    sweep query (ticket 13: real segments only)."""
    await helpers.seed_day(session_factory, test_date, cfg)
    async with session_factory() as session:
        from src.agent.edp.repository import seed_from_workflow  # noqa: F401 - post-trade rows
    # Create a post-trade row and fail it, then retry it.
    from src.agent.edp.utils.constants import POST_TRADE_ORDER

    code = POST_TRADE_ORDER[0]  # COLVAL
    async with session_factory() as session:
        wf = await repository.get_active(session, test_date)
        row = await repository.get_or_create(session, wf, test_date, code)
        await repository.move_to_state(session, row, SegmentStatus.FAILED, category="CBOS_ERROR", reason="forced")
        retried = await repository.retry_segment(session, test_date, code)
        await session.commit()

    assert retried is not None and retried.segment_status == SegmentStatus.PENDING
    assert retried.manually_activated is False, "post-trade rows must not be marked"

    async with session_factory() as session:
        rows = await repository.get_manually_activated_rows(session, min_date=test_date - timedelta(days=30))
        assert not any(r.segment_code == code for r in rows), "sweep query excludes post-trade"


async def test_same_day_missed_window_retry_is_not_insta_refailed(cfg, session_factory, test_date):
    """Round-2 review: an active-date retry after the window deadline was
    immediately re-FAILED as TIMEOUT by the normal path, making same-day
    retry useless until rollover. manually_activated rows are now exempt
    from the deadline insta-fail."""
    from datetime import time as dtime

    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    # EQ: same-day window (17:00-18:00) - 23:55 is unambiguously past its
    # deadline (MCX's window resolves to the NEXT morning, so it would read
    # as not-yet-open instead).
    await _fail_segment(session_factory, test_date, "EQ")
    async with session_factory() as session:
        await repository.retry_segment(session, test_date, "EQ")
        await session.commit()

    # Drive the NORMAL path with `now` far past the window deadline.
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_configured_codes = ("EQ",)
    late = datetime.combine(test_date, dtime(23, 55), tzinfo=orchestrator._tz)
    for _ in range(40):
        orchestrator._cycle_now = late
        await orchestrator._process_one_segment("EQ")
        async with session_factory() as session:
            row = await repository.get_one(session, test_date, "EQ")
        if row.segment_status in helpers.TERMINAL_STATES:
            break

    assert row.segment_status == SegmentStatus.COMPLETED, (
        f"expected the ops-requested retry to RUN past the deadline, got "
        f"{row.segment_status} ({row.skip_category}: {row.skip_reason})"
    )
