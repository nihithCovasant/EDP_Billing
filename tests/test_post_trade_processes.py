"""
T+1 post-trade processes — the 5-process chain (Collateral Valuation ->
Collateral Allocation -> MTF Fund Transfer -> Daily Margin Reporting ->
Daily Margin Statements) that runs once per trade_date, sequentially,
through the generic 3-step GTG -> trigger -> confirm pipeline.

Two scenarios, mirroring test_day1_all_segments_success.py /
test_day2_segment_process_failure.py for the 7-segment pipeline:
  1. All 5 post-trade processes complete successfully.
  2. The 2nd process (Collateral Allocation) fails partway through ->
     halts the remaining post-trade chain (MTFFT/DMRPT/DMSTMT stay PENDING).

Also covers: the chain is independent of the 7 real segments' status (no
segments are even seeded here), and Process 1's wall-clock window gate.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.repository import get_day_summary
from src.agent.edp.utils.constants import POST_TRADE_ORDER, get_sequence_order
from src.agent.edp.utils.datetime_utils import now_ist
from src.agent.edp.utils.locking import lock_state
from src.tools.cbos_client import CbosClient

from . import helpers
from .fakes import CountingPostTradeTriggerCbosClient, FailingCbosClient


async def test_all_post_trade_processes_complete_successfully(cfg, session_factory, test_date):
    """
    Deliberately does NOT seed/run the 7 real segments at all — the
    post-trade chain must be fully drivable on its own, per the
    "independent of segment status" design (see EdpOrchestrator class
    docstring / orchestrator._process_post_trade_chain()).
    """
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

        assert get_sequence_order(code) == 8 + POST_TRADE_ORDER.index(code)

    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)
    assert summary["total"] == 5
    assert summary["completed"] == 5


async def test_post_trade_process_failure_halts_remaining_chain(cfg, session_factory, test_date):
    """
    Collateral Allocation (2nd process)'s GTG check fails permanently ->
    it's marked FAILED, and MTF Fund Transfer / Daily Margin Reporting /
    Daily Margin Statements (3rd-5th) are never started (stay PENDING) —
    same halt-on-FAILED semantics as the 7-segment chain, scoped to just
    the post-trade chain.
    """
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
        assert row.segment_status == SegmentStatus.PENDING, (
            f"{code} should remain PENDING after the chain halts on COLALLOC, "
            f"got {row.segment_status}"
        )

    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)
    assert summary["total"] == 5
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["pending"] == 3


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


async def _prime_triggering_post_trade_row(session_factory, test_date, fixed_now, *, code: str = "COLVAL") -> None:
    """
    Simulate a pod crash right after handle_trigger_job() committed the
    pre-commit "TRIGGERING" marker but before the CBOS call's outcome was
    ever recorded — i.e. Steps up to GTG are done, phase=TRIGGER_JOB, and
    processes_json["trigger"] durably shows "TRIGGERING" from this test's
    perspective, without this test itself ever having called CBOS's
    trigger endpoint.
    """
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
    """
    Post-trade triggers have no CBOS-side status check (no PROCESSID/Table2
    equivalent like the 7-segment TRIGGER step has) — so an unconfirmed
    prior attempt can never be safely auto-recovered. Resuming into
    handle_trigger_job() with processes_json["trigger"]["status"] already
    "TRIGGERING" must mark the process FAILED (explicit "needs manual
    verification" reason) and must NEVER call the trigger endpoint again —
    the one behaviour that can't possibly double-trigger a real
    trading/collateral operation when no verification is possible.
    """
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

    # Chain halts here — nothing after COLVAL should have started.
    rows = await helpers.get_post_trade_rows(session_factory, test_date)
    for code in ("COLALLOC", "MTFFT", "DMRPT", "DMSTMT"):
        row = next(r for r in rows if r.segment_code == code)
        assert row.segment_status == SegmentStatus.PENDING


async def test_post_trade_stale_lock_at_triggering_is_unlocked_not_skipped(cfg, session_factory, test_date):
    """
    Same crash scenario as above, but via a genuinely stale (expired,
    never-released) lock rather than directly calling _process_one_post_trade
    — proving repository.recover_stale_locks() makes the same TRIGGER_JOB
    exception it makes for real segments' TRIGGER phase: unlock, don't
    silently SKIP, so the next cycle can reach the FAILED-with-explicit-
    reason outcome above instead of a generic AGENT_RESTART skip.
    """
    orchestrator = EdpOrchestrator(cfg, CountingPostTradeTriggerCbosClient(cfg.cbos_status_url, cfg.cbos_process_url))
    await helpers.seed_post_trade_day(session_factory, test_date)
    fixed_now = helpers.fixed_post_trade_now_for(test_date, orchestrator._tz)
    await _prime_triggering_post_trade_row(session_factory, test_date, fixed_now)

    real_now = now_ist()
    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "COLVAL")
        row.lock_json = {
            "state": "LOCKED",
            "owner": "dead-pod",
            "acquired_at": (real_now - timedelta(minutes=10)).isoformat(),
            "expires_at": (real_now - timedelta(minutes=5)).isoformat(),
        }
        await session.commit()

    async with session_factory() as session:
        affected = await repository.recover_stale_locks(session)
        await session.commit()
    assert affected == 1

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "COLVAL")
    assert row.segment_status == SegmentStatus.IN_PROGRESS, "must NOT be SKIPPED"
    assert row.current_phase == SegmentPhase.TRIGGER_JOB
    assert row.processes_json["trigger"]["status"] == "TRIGGERING"
    assert lock_state(row) == "UNLOCKED"
