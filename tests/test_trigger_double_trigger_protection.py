"""
Crash / double-trigger prevention for the Step 4 TRIGGER call.

"TRIGGERING" is written to processes_json BEFORE the CBOS call is made, so
the DB always leads the CBOS call, never follows it. If the process dies
anywhere between that write and the eventual TRIGGERED/FAILED write, the
segment resumes in state_machine.RealSegmentStateMachine._recover_trigger(),
which checks CBOS's own step statuses for the saved PROCESSID before
deciding whether it's safe to fire the trigger again.

These tests manipulate a segment_execution row directly to simulate a
crash at each of the two dangerous points, then drive it through
orchestrator._process_one_segment(), asserting on both DB state and the
number of real CBOS calls made.
"""

from __future__ import annotations

from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import SEGMENT_ORDER
from src.tools.cbos_client import CbosClient

from . import helpers
from .fakes import TransientTriggerFailureCbosClient

SEGMENT = "CUR"


async def _seed_and_prime_triggering_row(
    session_factory, cfg, orchestrator, test_date, process_id: str,
) -> None:
    """Seed a normal day, then rewrite the SEGMENT row into the state a
    crash would leave behind right after writing "TRIGGERING" to
    processes_json (Steps 1-2 done, phase=TRIGGER, marker written),
    without having actually called CBOS's trigger endpoint yet."""
    await helpers.seed_day(session_factory, test_date, cfg)
    fixed_now = helpers.fixed_now_for(test_date, orchestrator._tz)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = fixed_now
        row.process_id = process_id
        row.process_id_reserved_at = fixed_now
        row.current_phase = SegmentPhase.TRIGGER
        row.current_process = None
        row.processes_json = {
            "holiday_check": {"status": "COMPLETED", "last_response": "TRUE"},
            "file_upload_ready": {"status": "COMPLETED", "last_response": "TRUE"},
            "trigger": {
                "status": "TRIGGERING",
                "attempt_started_at": fixed_now.isoformat(),
                "process_id_source": "RESERVED_NEW",
            },
        }
        await session.commit()


async def test_recovery_retriggers_when_cbos_never_received_the_call(cfg, session_factory, test_date):
    """Crash BEFORE the CBOS trigger call was ever made. Recovery must
    conclude it's safe to fire the trigger for real, and the segment
    completes normally afterwards."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    fake_pid = "90001"

    await _seed_and_prime_triggering_row(session_factory, cfg, orchestrator, test_date, fake_pid)

    key = (SEGMENT, test_date.isoformat())
    assert key not in cbos._mock_trigger_calls, "sanity: CBOS must never have seen this PID before"

    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = helpers.fixed_now_for(test_date, orchestrator._tz)
    outcome = await orchestrator._process_one_segment(SEGMENT)
    assert outcome == "advanced"

    # 2 CBOS calls: recovery check (found nothing running) + the real trigger.
    assert cbos._mock_trigger_calls[key] == 2

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert row.current_phase == SegmentPhase.AWAIT_BILLPOSTING
    assert row.processes_json["trigger"]["status"] == "TRIGGERED"
    assert row.processes_json["trigger"]["process_id_source"] == "RESERVED_NEW"
    assert row.segment_status == SegmentStatus.IN_PROGRESS

    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    for code in SEGMENT_ORDER:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED


async def test_recovery_does_not_retrigger_when_cbos_already_has_it(cfg, session_factory, test_date):
    """Crash AFTER CBOS received the trigger call but BEFORE the DB write
    of TRIGGERED completed. Recovery must NOT fire the trigger a second
    time; it must just catch the DB up to TRIGGERED."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    fake_pid = "90002"

    # Simulate the "lost" trigger call actually reaching CBOS before the crash.
    pre_call = await cbos.get_new_trade_process(
        group_name=SEGMENT, login_id=cfg.cbos_login_id, trade_date=test_date, process_id=fake_pid,
    )
    assert pre_call.success

    await _seed_and_prime_triggering_row(session_factory, cfg, orchestrator, test_date, fake_pid)

    key = (SEGMENT, test_date.isoformat())
    assert cbos._mock_trigger_calls[key] == 1

    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = helpers.fixed_now_for(test_date, orchestrator._tz)
    outcome = await orchestrator._process_one_segment(SEGMENT)
    assert outcome == "advanced"

    # 1 more call — the recovery check itself; no retrigger happened.
    assert cbos._mock_trigger_calls[key] == 2

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert row.current_phase == SegmentPhase.AWAIT_BILLPOSTING
    assert row.processes_json["trigger"]["status"] == "TRIGGERED"
    assert row.processes_json["trigger"]["process_id_source"] == "RESERVED_NEW"

    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    for code in SEGMENT_ORDER:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED


async def test_resuming_in_progress_row_after_restart_reaches_recovery(cfg, session_factory, test_date):
    """Single-instance deployment: an IN_PROGRESS row simply resumes at its
    persisted current_phase on the next cycle. A segment that crashed
    mid-TRIGGERING resumes into handle_trigger()'s CBOS-checked recovery
    path and finishes on its own — no double trigger."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    fake_pid = "90003"

    await _seed_and_prime_triggering_row(session_factory, cfg, orchestrator, test_date, fake_pid)
    fixed_now = helpers.fixed_now_for(test_date, orchestrator._tz)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert row.segment_status == SegmentStatus.IN_PROGRESS
    assert row.current_phase == SegmentPhase.TRIGGER
    assert row.processes_json["trigger"]["status"] == "TRIGGERING"

    key = (SEGMENT, test_date.isoformat())
    assert key not in cbos._mock_trigger_calls, "sanity: CBOS never saw this PID before"
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = fixed_now
    outcome = await orchestrator._process_one_segment(SEGMENT)
    assert outcome == "advanced"
    # 2 CBOS calls: recovery check (found nothing running) + the real trigger.
    assert cbos._mock_trigger_calls[key] == 2

    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    for code in SEGMENT_ORDER:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED


async def test_transient_trigger_failure_stays_triggering_and_recovers(cfg, session_factory, test_date):
    """End-to-end, no manual DB manipulation: a transient error on the
    first real trigger call must leave processes_json["trigger"]["status"]
    as "TRIGGERING" (never FAILED) so the next cycle runs the same
    recovery path."""
    cbos = TransientTriggerFailureCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, fail_segment=SEGMENT,
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert by_code[SEGMENT].segment_status == SegmentStatus.COMPLETED
    assert by_code[SEGMENT].processes_json["trigger"]["status"] == "TRIGGERED"
    for code in SEGMENT_ORDER:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED
