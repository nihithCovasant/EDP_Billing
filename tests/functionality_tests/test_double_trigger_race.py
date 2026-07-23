"""
Two-pod double-trigger race on RealSegmentStateMachine.handle_triggered().

Since there's no pod-to-pod locking (single-instance deployment), the
TRIGGERING pre-commit marker is the only protection: handle_triggered()
does a plain unlocked SELECT, checks
processes_json["TRIGGERED"]["status"] == "TRIGGERING" in Python, then
writes "TRIGGERING" and commits. Between two independent DB sessions
(two pods), there's a window where both can load the row before either
commits "TRIGGERING", both pass the guard, and both fire the CBOS trigger.

This test forces that window open deterministically (same technique as
test_workflow_upload_race.py): two independent orchestrator instances each
in their own session, with an asyncio.Event pair patched into
repository.get_one() holding the first pod's SELECT until the second
pod's SELECT of the same pre-TRIGGERING row also completes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import src.agent.edp.orchestrator as orchestrator_module
from src.agent.edp import repository
from src.agent.edp.models import SegmentState
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient, NewTradeProcessResult

from .. import helpers

SEGMENT = "EQ"


class CountingRacyCbosClient(CbosClient):
    """
    Behaves exactly like CbosClient(use_mock=True) for every call, but for
    trigger-mode getNewTradeProcess calls (process_id != "0") on the target
    segment:
      - counts every real invocation (trigger_call_count), safe across
        concurrent coroutines since asyncio has no true parallelism —
        increments are never actually interleaved at the bytecode level.
      - the FIRST caller to arrive waits on `release_first` before
        completing the call, giving a concurrently-running second pod a
        window to independently reach and pass its own "already
        TRIGGERING" guard check before the first pod's DB commit of
        "TRIGGERED" lands.
    """

    def __init__(self, status_url: str, process_url: str, *, target_segment: str):
        super().__init__(status_url, process_url, use_mock=True)
        self._target_segment = target_segment.upper()
        self.trigger_call_count = 0
        self.first_call_entered = asyncio.Event()
        self.release_first = asyncio.Event()
        self._first_claimed = False

    async def get_new_trade_process(
        self,
        group_name: str,
        login_id: str,
        trade_date,
        process_id: str = "0",
    ) -> NewTradeProcessResult:
        is_trigger_mode = group_name.upper() == self._target_segment and process_id != "0"
        if is_trigger_mode:
            self.trigger_call_count += 1
            am_first = not self._first_claimed
            if am_first:
                self._first_claimed = True

            if am_first:
                # Announce we've entered the trigger call, then hold here
                # so the second pod gets a real chance to run its own
                # guard check + commit before we proceed.
                self.first_call_entered.set()
                await self.release_first.wait()
            else:
                # Second (or later) caller: let the first one go once we
                # ourselves have reached this point — proves both callers
                # were inside the CBOS call concurrently.
                self.release_first.set()

        return await super().get_new_trade_process(group_name, login_id, trade_date, process_id)


async def _drive_to_pre_triggering(session_factory, cfg, test_date) -> None:
    """
    Seed one real segment and drive it via the normal in-process CBOS mock
    (use_mock=True) from PENDING through INIT -> WAITING_FOR_FILE_UPLOAD ->
    TRIGGERED, stopping exactly before the first real trigger call — i.e.
    current_state == TRIGGERED but processes_json["TRIGGERED"] is not yet
    written (no "TRIGGERING" marker), matching the state
    handle_triggered() sees on its very first entry for this segment/day.
    """
    setup_cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    setup_cbos.mock_set_ready_after(1)
    setup_orchestrator = EdpOrchestrator(cfg, setup_cbos)

    await helpers.seed_day(session_factory, test_date, cfg)

    for _ in range(20):
        rows = await helpers.get_rows(session_factory, test_date)
        by_code = {r.segment_code: r for r in rows}
        row = by_code[SEGMENT]
        if row.current_state == SegmentState.TRIGGERED:
            triggered_state = row.processes_json.get(SegmentState.TRIGGERED.value, {})
            assert triggered_state.get("status") != "TRIGGERING", (
                "harness overshot: segment already has a TRIGGERING marker "
                "written — cannot set up the pre-trigger race window"
            )
            return

        setup_orchestrator._cycle_active_date = test_date
        setup_orchestrator._cycle_now = helpers.fixed_now_for(test_date, setup_orchestrator._tz, SEGMENT)
        await setup_orchestrator._process_one_segment(SEGMENT)

    raise TimeoutError(f"{SEGMENT} never reached TRIGGERED while driving to the pre-trigger window")


async def test_two_pods_can_both_pass_the_triggering_guard(cfg, session_factory, test_date):
    """
    Two independent orchestrator instances (two "pods"), each with their
    own DB session opened via repository.get_one(), both call
    _process_one_segment() for the SAME segment/trade_date concurrently
    while the row sits at TRIGGERED with no "TRIGGERING" marker yet.

    If handle_triggered()'s guard were safe against concurrent pods, only
    ONE of them could ever reach the real CBOS trigger call — the other
    would have to see "TRIGGERING" already set and go through
    _recover_trigger() instead. Since repository.get_one() is a plain
    unlocked SELECT and the guard check happens before any commit, both
    pods here should be able to independently observe "not TRIGGERING" and
    both call cbos.get_new_trade_process() for real.
    """
    await _drive_to_pre_triggering(session_factory, cfg, test_date)

    cbos = CountingRacyCbosClient(cfg.cbos_status_url, cfg.cbos_process_url, target_segment=SEGMENT)
    cbos.mock_set_ready_after(1)

    # Two independent orchestrators sharing the same racy CBOS client (so
    # the counter is shared / observable) but each simulating its own pod
    # by driving its own call into _process_one_segment(), which itself
    # opens an independent session via the module-level get_session() —
    # i.e. two separate DB sessions/connections reading the same row.
    pod_a = EdpOrchestrator(cfg, cbos)
    pod_b = EdpOrchestrator(cfg, cbos)

    fixed_now = helpers.fixed_now_for(test_date, pod_a._tz, SEGMENT)
    for pod in (pod_a, pod_b):
        pod._cycle_active_date = test_date
        pod._cycle_now = fixed_now

    results = []

    # --- Force the true race window open ------------------------------
    # _process_one_segment() does exactly one repository.get_one() SELECT
    # per pod before it ever reaches handle_triggered()'s guard check.
    # asyncio.gather() alone does NOT guarantee the two pods' SELECTs
    # interleave — without a real network/DB await boundary lining up,
    # one pod's whole single-state-transition call can run start-to-finish
    # (including its commit) before the other pod's SELECT is even issued,
    # which is exactly what a first attempt showed (trigger_call_count=1,
    # both pods report "advanced" because pod B's SELECT saw a row already
    # past TRIGGERED). To force true concurrency at the check-then-write
    # gap, we patch repository.get_one() (as imported into orchestrator.py)
    # so that the FIRST pod's SELECT deliberately waits for the SECOND
    # pod's SELECT to also complete before either pod proceeds any
    # further — i.e. both pods' independent reads of the SAME pre-TRIGGERING
    # row are guaranteed to land before either pod's guard check + commit.
    first_read_started = asyncio.Event()
    second_read_done = asyncio.Event()
    read_count = 0
    real_get_one = repository.get_one

    async def synchronized_get_one(session, trade_date, segment_code):
        nonlocal read_count
        row = await real_get_one(session, trade_date, segment_code)
        if segment_code == SEGMENT and row is not None and row.current_state == SegmentState.TRIGGERED:
            read_count += 1
            my_index = read_count
            if my_index == 1:
                first_read_started.set()
                # Hold pod A's read result until pod B has also read the
                # same pre-TRIGGERING row independently.
                try:
                    await asyncio.wait_for(second_read_done.wait(), timeout=10)
                except TimeoutError:
                    pass
            elif my_index == 2:
                second_read_done.set()
        return row

    async def run_pod(pod: EdpOrchestrator) -> None:
        outcome = await pod._process_one_segment(SEGMENT)
        results.append(outcome)

    with patch.object(orchestrator_module.repository, "get_one", side_effect=synchronized_get_one):
        await asyncio.gather(run_pod(pod_a), run_pod(pod_b))

    print(f"[RACE TEST] trigger_call_count={cbos.trigger_call_count} results={results}")

    async with session_factory() as verify_session:
        final_row = await repository.get_one(verify_session, test_date, SEGMENT)
    print(
        f"[RACE TEST] final current_state={final_row.current_state} "
        f"status={final_row.segment_status} "
        f"TRIGGERED_json={final_row.processes_json.get(SegmentState.TRIGGERED.value)}"
    )

    if cbos.trigger_call_count >= 2:
        print(
            "CONFIRMED: real double-trigger bug, no locking prevents it — "
            f"{cbos.trigger_call_count} independent trigger-mode getNewTradeProcess "
            f"calls were made for the same segment={SEGMENT} trade_date={test_date}."
        )
    else:
        print(
            f"Race did NOT reproduce this run (trigger_call_count={cbos.trigger_call_count}); "
            "either genuinely protected or this run's timing window wasn't tight enough."
        )

    assert cbos.trigger_call_count >= 2, (
        f"Expected both pods to independently pass the TRIGGERING guard and both fire "
        f"the real trigger call (>=2), got trigger_call_count={cbos.trigger_call_count}. "
        f"pod outcomes={results}"
    )
