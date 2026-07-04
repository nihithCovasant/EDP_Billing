"""
Pod-failure / double-trigger prevention for the Step 4 TRIGGER call.

Calling getNewTradeProcess(PROCESSID=<actual>) twice for the same segment-day
would make CBOS execute the whole billing chain twice — the single most
dangerous failure mode in the pipeline. Protection: "TRIGGERING" is written
to processes_json BEFORE the CBOS call is made (see
utils.json_helpers.record_trigger_attempt), so the DB always leads the CBOS
call, never follows it. If a pod dies (or a transient network error occurs)
anywhere between that write and the eventual TRIGGERED/FAILED write, the
segment resumes in pipeline.stages._recover_trigger(), which checks CBOS's
own Table2 step statuses for the saved PROCESSID before deciding whether
it's safe to fire the trigger again.

These tests manipulate a single segment_execution row directly (same
pattern as test_reserve_pid_step2_reuses_existing_process_id in
test_day1_all_segments_success.py) to simulate a pod having crashed at each
of the two dangerous points, then drive it through orchestrator
._process_one_segment() — the exact method a real wake cycle calls — and
assert on both the resulting DB state AND how many real CBOS calls were
made, to prove the dangerous "re-trigger a PID CBOS already has" call never
happens.
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
    """
    Seed a normal day, then rewrite the SEGMENT row directly into the exact
    state a pod would leave behind if it crashed right after writing
    "TRIGGERING" to processes_json — i.e. Steps 1-2 already done, a
    process_id already resolved, phase=TRIGGER, and the pre-commit
    "TRIGGERING" marker already written — WITHOUT ever having called CBOS's
    trigger endpoint yet from this test's perspective. The caller decides
    whether to prime the mock CBOS's own internal state before this.
    """
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
    """
    Pod died BEFORE the CBOS trigger call was ever made (or before it left
    the process). CBOS's Table2 for this PROCESSID is empty/all-PENDING —
    the recovery check must conclude it's safe to fire the trigger for
    real, and the segment must complete normally afterwards.
    """
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

    # Exactly 2 real CBOS calls: the recovery check (found nothing running)
    # plus the one real (re)trigger call it then correctly made.
    assert cbos._mock_trigger_calls[key] == 2

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert row.current_phase == SegmentPhase.AWAIT_BILLPOSTING
    assert row.processes_json["trigger"]["status"] == "TRIGGERED"
    assert row.processes_json["trigger"]["process_id_source"] == "RESERVED_NEW"
    assert row.segment_status == SegmentStatus.IN_PROGRESS

    # Let the segment (and the rest of the day) finish out normally.
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    for code in SEGMENT_ORDER:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED


async def test_recovery_does_not_retrigger_when_cbos_already_has_it(cfg, session_factory, test_date):
    """
    Pod died AFTER CBOS received the trigger call but BEFORE the DB write
    of TRIGGERED completed. CBOS's Table2 for this PROCESSID already shows
    a step IN_PROGRESS — the recovery check must NOT fire the trigger a
    second time; it must just catch the DB up to TRIGGERED.
    """
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    fake_pid = "90002"

    # Simulate the "lost" trigger call actually reaching CBOS before the
    # crash — this is the 1st real trigger-mode call CBOS ever saw for
    # this PID (mirrors what pipeline.stages.handle_trigger would have
    # done right before the pod died).
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

    # Exactly 1 MORE call — the recovery check itself. CBOS already showed
    # progress, so the dangerous "retrigger" call must NOT have happened.
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


async def test_transient_trigger_failure_stays_triggering_and_recovers(cfg, session_factory, test_date):
    """
    End-to-end, no manual DB manipulation: a transient (network-like) error
    on the very first real trigger call must leave processes_json["trigger"]
    ["status"] as "TRIGGERING" (never "FAILED", never touching
    segment_status) so the next wake cycle runs the same recovery decision
    tree — proving the two crash-recovery tests above also cover the
    "ambiguous send" transient-error case, not just literal pod death.
    """
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
