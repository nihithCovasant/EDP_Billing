"""
T+1 post-trade processes — the 5-process chain (COLVAL -> COLALLOC ->
MTFFT -> DMRPT -> DMSTMT) that runs once per trade_date through the
generic 3-step GTG -> trigger -> confirm pipeline.

Covers: happy path, a mid-chain failure not blocking independent
processes, the chain running standalone (no segments seeded), and the
default T+1 02:30 wall-clock window gate (applies to all 5 processes).
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.repository import get_day_summary
from src.agent.edp.utils.constants import POST_TRADE_ORDER, SEGMENT_ORDER, get_sequence_order
from src.tools.cbos_client import CbosClient

from . import helpers
from .fakes import CountingPostTradeTriggerCbosClient, FailingCbosClient


async def test_all_post_trade_processes_complete_successfully(cfg, session_factory, test_date):
    """Deliberately does NOT seed the 9 real segments — the post-trade
    chain must be fully drivable on its own."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)  # every poll succeeds first try -> fastest happy path
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_post_trade_day(session_factory, test_date)
    rows = await helpers.drive_post_trade_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert [r.segment_code for r in rows] == list(POST_TRADE_ORDER)
    assert set(by_code) == set(POST_TRADE_ORDER)

    for code in POST_TRADE_ORDER:
        row = by_code[code]
        assert row.segment_status == SegmentStatus.COMPLETED, (
            f"post-trade process {code} expected COMPLETED, got {row.segment_status} "
            f"(skip_category={row.skip_category!r} skip_reason={row.skip_reason!r})"
        )
        assert row.current_phase is not None and row.current_phase.value == "DONE"
        assert row.completed_at is not None
        assert row.started_at is not None
        assert row.skip_category is None
        assert row.skip_reason is None

        for stage_key in ("gtg", "trigger", "confirm"):
            assert stage_key in row.processes_json, f"{code} missing processes_json[{stage_key}]"
        assert row.processes_json["trigger"]["status"] == "TRIGGERED"
        assert row.processes_json["trigger"]["message"] == "Process started successfully"
        assert row.processes_json["confirm"]["status"] == "COMPLETED"

        assert get_sequence_order(code) == len(SEGMENT_ORDER) + 1 + POST_TRADE_ORDER.index(code)

    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)
    assert summary["total"] == 5
    assert summary["completed"] == 5


async def test_post_trade_process_failure_does_not_block_others(cfg, session_factory, test_date):
    """COLALLOC's GTG check fails permanently -> marked FAILED, but
    MTFFT/DMRPT/DMSTMT are independent and still run to COMPLETED."""
    cbos = FailingCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url,
        fail_segment="COLALLOC", fail_process="CollateralAllocation",
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_post_trade_day(session_factory, test_date)
    rows = await helpers.drive_post_trade_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    colval = by_code["COLVAL"]
    assert colval.segment_status == SegmentStatus.COMPLETED

    colalloc = by_code["COLALLOC"]
    assert colalloc.segment_status == SegmentStatus.FAILED
    assert colalloc.skip_category == "CBOS_ERROR"
    assert "CollateralAllocation" in (colalloc.skip_reason or "")

    for code in ("MTFFT", "DMRPT", "DMSTMT"):
        row = by_code[code]
        assert row.segment_status == SegmentStatus.COMPLETED, (
            f"{code} is independent of COLALLOC's failure and should still complete, "
            f"got {row.segment_status}"
        )

    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)
    assert summary["total"] == 5
    assert summary["completed"] == 4
    assert summary["failed"] == 1
    assert summary["pending"] == 0


async def test_post_trade_process1_window_gate(cfg, session_factory, test_date):
    """
    Process 1 (COLVAL) must not start before its 02:30 IST (trade_date+1)
    window opens — the orchestrator should report "blocked" and leave the
    row PENDING; once "now" is inside the window, it proceeds normally.
    """
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_post_trade_day(session_factory, test_date)

    orchestrator._cycle_active_date = test_date
    before_window = datetime.combine(
        test_date + timedelta(days=1), dtime(1, 0), tzinfo=orchestrator._tz
    )
    orchestrator._cycle_now = before_window
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "blocked"

    rows = await helpers.get_post_trade_rows(session_factory, test_date)
    colval = next(r for r in rows if r.segment_code == "COLVAL")
    assert colval.segment_status == SegmentStatus.PENDING

    orchestrator._cycle_now = helpers.fixed_post_trade_now_for(test_date, orchestrator._tz)
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome in ("advanced", "completed")


async def test_all_post_trade_processes_gated_by_default_when_no_window_configured(
    cfg, session_factory, test_date,
):
    """
    Every one of the 5 post-trade processes — not just COLVAL — must default
    to the 02:30 IST (T+1) gate when workflow_json doesn't specify its own
    window_start. Regression test for a bug where COLALLOC/MTFFT/DMRPT/
    DMSTMT started (and called CBOS) immediately, same-day, ungated.
    """
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_post_trade_day(session_factory, test_date)

    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = datetime.combine(test_date, dtime(15, 0), tzinfo=orchestrator._tz)

    for code in POST_TRADE_ORDER:
        outcome = await orchestrator._process_one_post_trade(code)
        assert outcome == "blocked", f"{code} must be gated before 02:30 T+1, got {outcome}"

    rows = await helpers.get_post_trade_rows(session_factory, test_date)
    assert all(r.segment_status == SegmentStatus.PENDING for r in rows)


async def _prime_triggering_post_trade_row(session_factory, test_date, fixed_now, *, code: str = "COLVAL") -> None:
    """Simulate a crash right after handle_trigger_job() committed the
    pre-commit "TRIGGERING" marker but before the outcome was recorded."""
    async with session_factory() as session:
        row = await repository.get_one(session, test_date, code)
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = fixed_now
        row.current_phase = SegmentPhase.TRIGGER_JOB
        row.current_process = None
        row.processes_json = {
            "gtg": {"status": "COMPLETED", "last_response": "TRUE"},
            "trigger": {"status": "TRIGGERING", "attempt_started_at": fixed_now.isoformat()},
        }
        await session.commit()


async def test_post_trade_trigger_resume_fails_instead_of_retriggering(cfg, session_factory, test_date):
    """Post-trade triggers have no CBOS-side status check, so an
    unconfirmed prior attempt can never be safely auto-recovered. Resuming
    with "TRIGGERING" already set must mark the process FAILED and must
    NEVER call the trigger endpoint again."""
    cbos = CountingPostTradeTriggerCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_post_trade_day(session_factory, test_date)
    fixed_now = helpers.fixed_post_trade_now_for(test_date, orchestrator._tz)
    await _prime_triggering_post_trade_row(session_factory, test_date, fixed_now)

    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = fixed_now
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "failed"
    assert cbos.trigger_call_count == 0, "must NOT call the trigger endpoint again"

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "COLVAL")
    assert row.segment_status == SegmentStatus.FAILED
    assert row.skip_category == "CBOS_ERROR"
    assert "manual" in (row.skip_reason or "").lower() or "verif" in (row.skip_reason or "").lower()
    assert row.processes_json["trigger"]["status"] == "TRIGGERING", (
        "the unresolved marker itself must be left alone for forensics — "
        "only segment_status/skip_reason record the FAILED outcome"
    )

    # Only COLVAL was driven in this test (single _process_one_post_trade
    # call) — the rest were never touched, hence still PENDING.
    rows = await helpers.get_post_trade_rows(session_factory, test_date)
    for code in ("COLALLOC", "MTFFT", "DMRPT", "DMSTMT"):
        row = next(r for r in rows if r.segment_code == code)
        assert row.segment_status == SegmentStatus.PENDING
