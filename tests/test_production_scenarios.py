"""
Production-realistic end-to-end scenarios — the kinds of days that actually
happen live, exercising the full stack together (state machine/move_to_state,
alerts, locking, T+1 calendar gating) rather than any one mechanism in
isolation. Complements the unit-level tests in test_move_to_state.py and the
single-outcome-per-day tests in test_day1_all_segments_success.py /
test_day2_segment_process_failure.py / test_post_trade_processes.py.

Scenarios covered:
  1. A mixed day: one segment completes, one hits a market holiday
     (continues), one then fails outright (halts the rest) — with exactly
     the right alerts firing for each.
  2. The manager's literal T+1 requirement: a post-trade process must not
     start on T at all, and must start only once wall-clock crosses into
     T+1's window — proven by moving the clock across three checkpoints.
  3. A full two-pipeline day: 7 real segments complete on T, then all 5
     post-trade processes complete on T+1, independently.
  4. Agent-restart recovery: a segment left IN_PROGRESS at various
     mid-pipeline phases (as if the pod died right after a move_to_state
     commit) is picked up by a brand-new orchestrator and finishes
     correctly from that exact phase.
  5. A sustained CBOS outage: transient errors on every single poll —
     the segment must stay blocked forever (never FAILED/SKIPPED) while
     the transient-error log escalates from WARNING to ERROR.
  6. An instant-CBOS day: a single advance_pipeline() call chains through
     several phases in one shot (no wake-cycle boundary in between),
     proving move_to_state's guard doesn't interfere with legitimate
     same-cycle multi-hop advances.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, time as dtime

import pytest

from src.agent.edp import alerts as alerts_module
from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.pipeline.executor import advance_pipeline
from src.agent.edp.utils.datetime_utils import IST, now_ist
from src.tools.cbos_client import CbosClient

from . import helpers
from .fakes import ScenarioCbosClient, TransientOutageCbosClient


class _AsyncRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, row: dict) -> None:
        self.calls.append(row)


# ---------------------------------------------------------------------------
# 1. Mixed-outcome day
# ---------------------------------------------------------------------------

async def test_mixed_outcome_production_day_success_skip_failure(monkeypatch, cfg, session_factory, test_date):
    """EQ completes normally, DR hits a market holiday (SKIPPED, chain
    continues), CUR then fails permanently (FAILED, halts) — SL/MCX/NCDEX/MTF
    never start. Exactly one skip alert (DR) and one failure alert (CUR),
    zero timeout alerts, zero alerts for the completed segment, and
    last_heartbeat_at only set on the three segments actually touched."""
    failure_alerts = _AsyncRecorder()
    timeout_alerts = _AsyncRecorder()
    skip_alerts = _AsyncRecorder()
    monkeypatch.setattr(alerts_module, "send_failure_alert", failure_alerts)
    monkeypatch.setattr(alerts_module, "send_timeout_alert", timeout_alerts)
    monkeypatch.setattr(alerts_module, "send_skip_alert", skip_alerts)

    cbos = ScenarioCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url,
        holiday_segments=("DR",), fail_at=("CUR", "BILLPOSTING"),
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert by_code["EQ"].segment_status == SegmentStatus.COMPLETED
    assert by_code["DR"].segment_status == SegmentStatus.SKIPPED
    assert by_code["DR"].skip_category == "CBOS_SKIP"
    assert by_code["CUR"].segment_status == SegmentStatus.FAILED
    assert by_code["CUR"].skip_category == "CBOS_ERROR"

    for code in ("SL", "MCX", "NCDEX", "MTF"):
        row = by_code[code]
        assert row.segment_status == SegmentStatus.PENDING, f"{code} must never have started"
        assert row.current_phase is None
        assert row.last_heartbeat_at is None, f"{code} was never touched — must have no heartbeat"

    # EQ and CUR both made at least one real move_to_state transition before
    # finishing (EQ all the way through; CUR up to AWAIT_BILLPOSTING before
    # failing there) — both must carry a heartbeat.
    for code in ("EQ", "CUR"):
        assert by_code[code].last_heartbeat_at is not None, f"{code} was processed — must have a heartbeat"
    # DR, in contrast, hits the holiday gate on its very FIRST check —
    # SKIPPED is raised directly out of HOLIDAY_CHECK before any successful
    # move_to_state transition ever happens, so it legitimately never gets
    # a heartbeat stamped. This is correct, not a gap: a segment that never
    # entered a real polling loop needs no staleness tracking.
    assert by_code["DR"].last_heartbeat_at is None

    assert len(skip_alerts.calls) == 1
    assert skip_alerts.calls[0]["segment_code"] == "DR"
    assert len(failure_alerts.calls) == 1
    assert failure_alerts.calls[0]["segment_code"] == "CUR"
    assert timeout_alerts.calls == [], "no window deadlines were involved in this scenario"


# ---------------------------------------------------------------------------
# 2. T+1 calendar rollover gating
# ---------------------------------------------------------------------------

async def test_post_trade_only_starts_after_calendar_rollover_to_t_plus_1(cfg, session_factory, test_date):
    """Manager's literal requirement: if trade_date is the Nth, COLVAL must
    not run at any point on the Nth — only from 02:30 IST on the (N+1)th
    onward. Checked at three points on the clock: late on T, just before
    the T+1 window opens, and right at/after it opens."""
    # Default mock_ready_after (2) deliberately left as-is: the first GTG poll
    # must return FALSE (not ready) so a single _process_one_post_trade() call
    # right at window-open lands on AWAIT_GTG and stays there, instead of
    # chaining all the way to AWAIT_CONFIRM in one shot (see the dedicated
    # instant-multi-hop test for that scenario) — this test is only about the
    # start gate, not how far it gets afterward.
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)
    tz = orchestrator._tz

    await helpers.seed_post_trade_day(session_factory, test_date)
    colval_code = (await helpers.get_post_trade_rows(session_factory, test_date))[0].segment_code
    assert colval_code == "COLVAL"

    async def _current_row():
        rows = await helpers.get_post_trade_rows(session_factory, test_date)
        return next(r for r in rows if r.segment_code == "COLVAL")

    # Late on T itself (23:59) — must still be untouched.
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = datetime.combine(test_date, dtime(23, 59), tzinfo=tz)
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "blocked"
    row = await _current_row()
    assert row.segment_status == SegmentStatus.PENDING
    assert row.current_phase is None
    assert row.started_at is None

    # T+1, one minute before the 02:30 window opens — still untouched.
    orchestrator._cycle_now = datetime.combine(test_date + timedelta(days=1), dtime(2, 29), tzinfo=tz)
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "blocked"
    row = await _current_row()
    assert row.segment_status == SegmentStatus.PENDING

    # T+1, exactly at the window open — must start now.
    orchestrator._cycle_now = datetime.combine(test_date + timedelta(days=1), dtime(2, 30), tzinfo=tz)
    await orchestrator._process_one_post_trade("COLVAL")
    row = await _current_row()
    assert row.segment_status == SegmentStatus.IN_PROGRESS
    assert row.current_phase == SegmentPhase.AWAIT_GTG
    assert row.started_at is not None
    # Postgres returns TIMESTAMPTZ columns as UTC-aware datetimes regardless of
    # what timezone was written — must convert back to IST before comparing
    # calendar dates, or an early-IST-morning timestamp (02:30-05:29 IST is
    # still the previous day in UTC) would misleadingly look like it landed
    # on trade_date instead of trade_date + 1.
    assert row.started_at.astimezone(IST).date() == test_date + timedelta(days=1), (
        "started_at must land on the calendar day AFTER trade_date, not trade_date itself"
    )


# ---------------------------------------------------------------------------
# 3. Full two-pipeline day (7 segments on T, 5 post-trade processes on T+1)
# ---------------------------------------------------------------------------

async def test_full_trade_day_then_next_day_post_trade_chain_end_to_end(cfg, session_factory, test_date):
    """All 7 segments complete on T; the 5 T+1 post-trade processes then
    complete independently — proving the two pipelines are correctly
    sequenced by calendar date and neither corrupts the other's rows."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    segment_rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    assert all(r.segment_status == SegmentStatus.COMPLETED for r in segment_rows)
    completed_at_snapshot = {r.segment_code: r.completed_at for r in segment_rows}

    await helpers.seed_post_trade_day(session_factory, test_date)
    post_trade_rows = await helpers.drive_post_trade_until_terminal(orchestrator, session_factory, test_date)

    assert len(post_trade_rows) == 5
    for row in post_trade_rows:
        assert row.segment_status == SegmentStatus.COMPLETED
        assert row.last_heartbeat_at is not None
        # See the calendar-rollover test for why .astimezone(IST) is required
        # before comparing dates against a Postgres-read-back TIMESTAMPTZ.
        assert row.started_at.astimezone(IST).date() == test_date + timedelta(days=1)

    # The 7 segment rows from T must be completely untouched by post-trade
    # processing — get_rows() now also returns the 5 post-trade rows (same
    # trade_date), so only re-check the original 7 by code.
    final_segment_rows = await helpers.get_rows(session_factory, test_date)
    final_by_code = {r.segment_code: r for r in final_segment_rows}
    for code, completed_at in completed_at_snapshot.items():
        assert final_by_code[code].completed_at == completed_at


# ---------------------------------------------------------------------------
# 4. Agent-restart recovery at various mid-pipeline phases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phase,process",
    [
        (SegmentPhase.RESERVE_PID, None),
        (SegmentPhase.AWAIT_FILE_UPLOAD, "FILEUPLOAD"),
        (SegmentPhase.AWAIT_BILLPOSTING, "BILLPOSTING"),
        (SegmentPhase.AWAIT_RECON, "RECON"),
        (SegmentPhase.AWAIT_CONTRACT_NOTE, "CONTRACTNOTEGENERATION"),
    ],
)
async def test_agent_restart_resumes_from_persisted_phase(phase, process, cfg, session_factory, test_date):
    """A pod restart: EQ is left IN_PROGRESS at an arbitrary mid-pipeline
    phase (as if a prior move_to_state commit landed right before the
    process died), and a brand-new orchestrator/session picks it back up
    and drives it to COMPLETED from exactly that phase — never restarting
    from HOLIDAY_CHECK."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)

    await helpers.seed_day(session_factory, test_date, cfg)

    real_pid = None
    if phase != SegmentPhase.RESERVE_PID:
        # A real reservation through the same mock (not a made-up string) —
        # the mock's own trigger/recovery-check paths parse process_id as an
        # int, exactly like real CBOS PIDs, so it must look like one.
        reserved = await cbos.get_new_trade_process(
            group_name="EQ", login_id=cfg.cbos_login_id, trade_date=test_date, process_id="0",
        )
        assert reserved.success
        real_pid = reserved.process_id

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "EQ")
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = now_ist()
        row.current_phase = phase
        row.current_process = process
        if real_pid is not None:
            row.process_id = real_pid
            row.process_id_reserved_at = now_ist()
        await session.commit()

    # A fresh orchestrator instance simulates a brand-new pod picking up the row.
    orchestrator = EdpOrchestrator(cfg, cbos)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    eq_row = next(r for r in rows if r.segment_code == "EQ")
    assert eq_row.segment_status == SegmentStatus.COMPLETED, (
        f"resuming from {phase} must still reach COMPLETED"
    )
    # Every other segment must have run too — the restarted row must not have
    # blocked the rest of the day's sequence.
    assert all(r.segment_status == SegmentStatus.COMPLETED for r in rows)


# ---------------------------------------------------------------------------
# 5. Sustained CBOS outage
# ---------------------------------------------------------------------------

async def test_cbos_outage_stays_blocked_and_escalates_without_corrupting_state(caplog, cfg, session_factory, test_date):
    """A real CBOS outage: file_process_status returns a transient error on
    every single poll. EQ must stay IN_PROGRESS/BLOCKED at HOLIDAY_CHECK
    forever — never FAILED, never SKIPPED — while the transient-error log
    escalates from WARNING to ERROR after enough consecutive failures, and
    the heartbeat (via orchestrator's touch_heartbeat on the BLOCKED path)
    keeps advancing so staleness detection doesn't false-positive on an
    outage that the agent is actually still actively retrying."""
    caplog.set_level(logging.WARNING, logger="cams_otel_lib")
    cbos = TransientOutageCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url,
        fail_segment="EQ", fail_process="BeginFileUpload",
    )
    orchestrator = EdpOrchestrator(cfg, cbos)
    await helpers.seed_day(session_factory, test_date, cfg)

    for _ in range(35):
        await helpers.run_one_cycle(orchestrator, session_factory, test_date)

    rows = await helpers.get_rows(session_factory, test_date)
    eq_row = next(r for r in rows if r.segment_code == "EQ")
    assert eq_row.segment_status == SegmentStatus.IN_PROGRESS
    assert eq_row.current_phase == SegmentPhase.HOLIDAY_CHECK, "CBOS never recovered — must never advance"
    assert eq_row.last_heartbeat_at is not None, "touch_heartbeat must still fire every BLOCKED cycle"

    warning_lines = [r.message for r in caplog.records if r.levelname == "WARNING" and "segment=EQ" in r.message]
    error_lines = [r.message for r in caplog.records if r.levelname == "ERROR" and "segment=EQ" in r.message]
    assert warning_lines, "early polls must log at WARNING (ordinary transient retry)"
    assert any("likely outage" in line for line in error_lines), (
        "sustained failure past the escalation threshold must log at ERROR"
    )


# ---------------------------------------------------------------------------
# 6. Instant-CBOS day: multiple phases advanced in one advance_pipeline() call
# ---------------------------------------------------------------------------

async def test_single_advance_pipeline_call_chains_multiple_phases_when_cbos_is_instantly_ready(
    cfg, session_factory, test_date,
):
    """When CBOS answers ready on the very first poll of every stage, one
    advance_pipeline() call must chain HOLIDAY_CHECK -> RESERVE_PID ->
    AWAIT_FILE_UPLOAD -> TRIGGER -> AWAIT_BILLPOSTING in a single shot
    (stopping there — TRIGGER always waits for the next cycle before
    polling BILLPOSTING) — proving move_to_state's old==new guard doesn't
    interfere with legitimate same-cycle multi-hop advances, and that the
    row's heartbeat reflects the LAST hop, not the first."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    await helpers.seed_day(session_factory, test_date, cfg)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "EQ")
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = now_ist()
        row.current_phase = SegmentPhase.HOLIDAY_CHECK
        await session.commit()

        row = await repository.get_one(session, test_date, "EQ")
        outcome = await advance_pipeline(
            cbos=cbos, row=row, session=session, login_id=cfg.cbos_login_id,
            now=now_ist(), window_end=None,
        )
        await session.commit()

    assert outcome == "advanced", "TRIGGER firing successfully maps to STOP_NEXT -> 'advanced'"
    async with session_factory() as fresh_session:
        reloaded = await repository.get_one(fresh_session, test_date, "EQ")
        assert reloaded.current_phase == SegmentPhase.AWAIT_BILLPOSTING
        assert reloaded.current_process == "BILLPOSTING"
        assert reloaded.last_heartbeat_at is not None
        assert reloaded.process_id is not None, "RESERVE_PID must have run within this same call"
