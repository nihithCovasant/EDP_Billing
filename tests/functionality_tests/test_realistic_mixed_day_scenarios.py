"""
Realistic mixed full-day operational scenarios — driving the REAL
EdpOrchestrator + real DB (via tests/helpers.py's harness) through the kind
of heterogeneous day ops actually sees: a subset of segments on holiday
while others trade, simultaneous failures at different pipeline stages
recovered via retry, a slow CBOS day, a manual ops skip mid-day, and the
end-of-day summary's accuracy against a mixed outcome set.

Follows the exact conventions of test_day1_all_segments_success.py /
test_day2_segment_process_failure.py / test_post_trade_processes.py: real
orchestrator, real DB, CbosClient's in-process mock (or fakes.py-style thin
subclasses of it), helpers.seed_day()/drive_until_terminal(), precise
assertions on final states/categories/reasons/call counts/summary counts.
"""

from __future__ import annotations

from datetime import date

from src.agent.edp import repository
from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.repository import get_day_summary
from src.agent.edp.utils.constants import SEGMENT_ORDER
from src.tools.cbos_client import CbosClient, FileStatusResult

from .. import helpers
from ..fakes import FailingCbosClient

# =============================================================================
# Fakes specific to these scenarios (extending fakes.py's patterns/naming).
# =============================================================================


class MultiSkippingCbosClient(CbosClient):
    """
    Like fakes.SkippingCbosClient, but for a realistic MIXED holiday day —
    a SET of segments return SKIP on their INIT holiday check (BeginFileUpload)
    while every other segment behaves exactly like the normal in-process mock.
    Models the real scenario where a currency/equity holiday doesn't close
    commodity markets (NCDEX/MCX/NSECOM keep trading).
    """

    def __init__(self, status_url: str, process_url: str, *, skip_segments: set[str]):
        super().__init__(status_url, process_url, use_mock=True)
        self._skip_segments = {s.upper() for s in skip_segments}

    async def file_process_status(
        self, segment: str, process_name: str, user_id: str
    ) -> FileStatusResult:
        if segment.upper() in self._skip_segments and process_name == "BeginFileUpload":
            return FileStatusResult(response="SKIP", raw_body='{"Status":"SKIP"}', error=None, is_transient=False)
        return await super().file_process_status(segment, process_name, user_id)


class MultiFailingCbosClient(CbosClient):
    """
    Like fakes.FailingCbosClient, but supports TWO independent (segment,
    process) permanent-failure pairs simultaneously — used to prove two
    segments failing at two DIFFERENT pipeline stages in the same day are
    handled completely independently of one another.
    """

    def __init__(self, status_url: str, process_url: str, *, fail_pairs: dict[str, str]):
        super().__init__(status_url, process_url, use_mock=True)
        self._fail_pairs = {seg.upper(): proc for seg, proc in fail_pairs.items()}

    async def file_process_status(
        self, segment: str, process_name: str, user_id: str
    ) -> FileStatusResult:
        if self._fail_pairs.get(segment.upper()) == process_name:
            return FileStatusResult(
                response="FALSE",
                raw_body='{"Status":"CBOS_INTERNAL_ERROR"}',
                error=f"Simulated permanent CBOS failure for {segment}/{process_name}",
                is_transient=False,
            )
        return await super().file_process_status(segment, process_name, user_id)


class CountingFileStatusCbosClient(CbosClient):
    """
    Like fakes.RecordingFileStatusCbosClient, but exposes a per-segment call
    COUNT (not just the raw call log) — used by the manual-skip scenario to
    prove a segment's CBOS call count freezes the instant it's manually
    skipped, with no further calls on any later cycle.
    """

    def __init__(self, status_url: str, process_url: str):
        super().__init__(status_url, process_url, use_mock=True)
        self.call_counts: dict[str, int] = {}

    async def file_process_status(
        self, segment: str, process_name: str, user_id: str
    ) -> FileStatusResult:
        key = segment.upper()
        self.call_counts[key] = self.call_counts.get(key, 0) + 1
        return await super().file_process_status(segment, process_name, user_id)


# =============================================================================
# Scenario 1 — Mixed holiday day.
# =============================================================================

HOLIDAY_SEGMENTS = {"EQ", "DR", "CUR"}


async def test_mixed_holiday_day_some_segments_skip_others_complete(cfg, session_factory, test_date):
    """
    EQ/DR/CUR (equity/F&O/currency) are on holiday (CBOS SKIP at INIT);
    SLB/NCDEX/NCDEXPHY/MCX/MCXPHY/NSECOM are normal trading days and must
    complete independently. The post-trade chain afterward must still run
    to completion regardless of the mixed per-segment outcome mix.
    """
    cbos = MultiSkippingCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, skip_segments=HOLIDAY_SEGMENTS,
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert set(by_code) == set(SEGMENT_ORDER)

    for code in SEGMENT_ORDER:
        row = by_code[code]
        if code in HOLIDAY_SEGMENTS:
            assert row.segment_status == SegmentStatus.SKIPPED, (
                f"segment {code} expected SKIPPED (holiday), got {row.segment_status}"
            )
            assert row.skip_category == "CBOS_SKIP"
            assert "holiday" in (row.skip_reason or "").lower()
            assert row.current_state is None
        else:
            assert row.segment_status == SegmentStatus.COMPLETED, (
                f"segment {code} expected COMPLETED (normal trading day), got {row.segment_status} "
                f"(skip_category={row.skip_category!r} skip_reason={row.skip_reason!r})"
            )
            assert row.skip_category is None
            assert row.skip_reason is None

    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)
    assert summary["total"] == len(SEGMENT_ORDER)
    assert summary["skipped"] == len(HOLIDAY_SEGMENTS)
    assert summary["completed"] == len(SEGMENT_ORDER) - len(HOLIDAY_SEGMENTS)
    assert summary["failed"] == 0
    assert summary["pending"] == 0
    assert summary["in_progress"] == 0

    # Post-trade chain still runs to completion regardless of the mixed day.
    await helpers.seed_post_trade_day(session_factory, test_date)
    pt_rows = await helpers.drive_post_trade_until_terminal(orchestrator, session_factory, test_date)
    for row in pt_rows:
        assert row.segment_status == SegmentStatus.COMPLETED, (
            f"post-trade process {row.segment_code} expected COMPLETED after a mixed "
            f"holiday/trading day, got {row.segment_status}"
        )


# =============================================================================
# Scenario 2 — Partial pipeline failure at two different stages, then retry.
# =============================================================================

FAIL_PAIRS = {"EQ": "BILLPOSTING", "NCDEX": "RECON"}


async def test_partial_pipeline_failure_two_segments_two_stages_then_retry_recovers(
    cfg, session_factory, test_date,
):
    """
    EQ fails at BILLPOSTING (order=2), NCDEX fails at RECON (order=4) —
    different segments, different pipeline stages, in the same day. Both
    must end FAILED with current_state/current_process frozen at their own
    failure point (diagnostics), independent of one another. Then
    repository.retry_segment() for BOTH (with a healthy CBOS fake swapped
    in) must bring both to COMPLETED.
    """
    failing_cbos = MultiFailingCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, fail_pairs=FAIL_PAIRS,
    )
    failing_cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, failing_cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    eq_row = by_code["EQ"]
    assert eq_row.segment_status == SegmentStatus.FAILED
    assert eq_row.skip_category == "CBOS_ERROR"
    assert "BILLPOSTING" in eq_row.skip_reason
    assert eq_row.current_state == SegmentState.WAITING_FOR_BILLPOSTING, (
        "FAILED leaves current_state frozen where EQ's pipeline broke, for diagnostics"
    )

    ncdex_row = by_code["NCDEX"]
    assert ncdex_row.segment_status == SegmentStatus.FAILED
    assert ncdex_row.skip_category == "CBOS_ERROR"
    assert "RECON" in ncdex_row.skip_reason
    assert ncdex_row.current_state == SegmentState.WAITING_FOR_RECON, (
        "FAILED leaves current_state frozen where NCDEX's pipeline broke, for diagnostics"
    )

    # Every other segment is independent of both failures and completes normally.
    other_codes = [c for c in SEGMENT_ORDER if c not in FAIL_PAIRS]
    for code in other_codes:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED, (
            f"segment {code} expected COMPLETED (independent of EQ/NCDEX failures), "
            f"got {by_code[code].segment_status}"
        )

    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)
    assert summary["failed"] == 2
    assert summary["completed"] == len(SEGMENT_ORDER) - 2
    assert summary["pending"] == 0
    assert summary["in_progress"] == 0
    assert summary["skipped"] == 0

    # --- Ops retries BOTH failed segments now that CBOS is healthy again ---
    async with session_factory() as session:
        retried_eq = await repository.retry_segment(session, test_date, "EQ")
        retried_ncdex = await repository.retry_segment(session, test_date, "NCDEX")
        await session.commit()

    for retried in (retried_eq, retried_ncdex):
        assert retried is not None
        assert retried.segment_status == SegmentStatus.PENDING
        assert retried.current_state is None
        assert retried.skip_category is None
        assert retried.skip_reason is None
        assert retried.processes_json == {}

    healthy_cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    healthy_cbos.mock_set_ready_after(1)
    orchestrator.cbos = healthy_cbos

    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    for code in SEGMENT_ORDER:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED, (
            f"segment {code} expected COMPLETED after retrying EQ and NCDEX, "
            f"got {by_code[code].segment_status}"
        )


# =============================================================================
# Scenario 3 — Slow day: every segment needs multiple polls before ready.
# =============================================================================

async def test_slow_day_all_segments_need_multiple_polls_latest_response_recorded(
    cfg, session_factory, test_date,
):
    """
    Every one of the 9 segments requires several polls (mock_set_ready_after)
    before each CBOS check returns ready — simulating a slow, sluggish CBOS
    day. drive_until_terminal()'s max_cycles safety net must still reach
    all-terminal within a bounded, reasonable number of cycles (not needing
    an absurdly high max_cycles), and each segment's processes_json must
    show the LATEST poll response recorded at each stage, not stale earlier
    data (record_poll()/mark_step_done() always overwrite last_response).
    """
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    POLLS_BEFORE_READY = 4
    cbos.mock_set_ready_after(POLLS_BEFORE_READY)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)

    # 6 states per segment, each needing up to POLLS_BEFORE_READY cycles to
    # clear -> a bounded, reasonable cap well short of drive_until_terminal's
    # default 150 -- proves the day finishes quickly even though every stage
    # is slow, not just eventually within an enormous cap.
    bounded_max_cycles = 6 * POLLS_BEFORE_READY + 10
    rows = await helpers.drive_until_terminal(
        orchestrator, session_factory, test_date, max_cycles=bounded_max_cycles,
    )
    by_code = {r.segment_code: r for r in rows}

    for code in SEGMENT_ORDER:
        row = by_code[code]
        assert row.segment_status == SegmentStatus.COMPLETED, (
            f"segment {code} expected COMPLETED on a slow day, got {row.segment_status} "
            f"(skip_category={row.skip_category!r} skip_reason={row.skip_reason!r})"
        )

        # Each polling stage must show the terminal ("TRUE"/ready) response
        # as its last_response, never a stale earlier "FALSE" (not-yet-ready)
        # left behind from an intermediate poll.
        init_steps = row.processes_json[SegmentState.INIT.value]["steps"]
        init_step = init_steps.get("BeginFileUpload_STATUS", init_steps.get("BeginFileUpload"))
        assert init_step is not None, f"{code} missing INIT step data"
        assert init_step["last_response"] == "TRUE", (
            f"{code}'s INIT step must record the LATEST (ready) response, "
            f"got {init_step['last_response']!r}"
        )

        for state in (
            SegmentState.WAITING_FOR_BILLPOSTING,
            SegmentState.WAITING_FOR_RECON,
            SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION,
        ):
            state_dict = row.processes_json[state.value]
            assert state_dict["status"] == "COMPLETED"
            steps = state_dict["steps"]
            assert steps, f"{code}'s {state.value} has no recorded step data"
            for step_key, step in steps.items():
                assert step["last_response"] == "TRUE", (
                    f"{code}'s {state.value}[{step_key}] must show the LATEST poll "
                    f"response (TRUE), got {step['last_response']!r} — stale data left behind"
                )


# =============================================================================
# Scenario 4 — Manual skip mid-day while others are still PENDING/IN_PROGRESS.
# =============================================================================

MANUAL_SKIP_SEGMENT = "NCDEXPHY"


async def test_manual_skip_mid_day_freezes_call_count_others_unaffected(cfg, session_factory, test_date):
    """
    While the day is still in flight (some segments PENDING/IN_PROGRESS),
    ops manually skips one segment (non-holiday reason). It must become
    SKIPPED/MANUAL_SKIP immediately regardless of CBOS state, every other
    segment must proceed independently and unaffected, and the manually
    skipped segment's CBOS call count must freeze at the moment of the
    skip — no further calls on any later cycle.
    """
    cbos = CountingFileStatusCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    # Slow enough that segments are still mid-flight (PENDING/IN_PROGRESS)
    # when we intervene with the manual skip.
    cbos.mock_set_ready_after(3)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)

    # Run a couple of cycles so the day is genuinely in flight (not just seeded).
    await helpers.run_one_cycle(orchestrator, session_factory, test_date)
    await helpers.run_one_cycle(orchestrator, session_factory, test_date)

    rows = await helpers.get_rows(session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    assert not repository.is_handled(by_code[MANUAL_SKIP_SEGMENT]), (
        "test assumes NCDEXPHY is still PENDING/IN_PROGRESS at the moment of manual skip"
    )
    some_other_in_flight = [
        c for c in SEGMENT_ORDER
        if c != MANUAL_SKIP_SEGMENT and not repository.is_handled(by_code[c])
    ]
    assert some_other_in_flight, "test assumes at least one other segment is also still in flight"

    calls_before_skip = cbos.call_counts.get(MANUAL_SKIP_SEGMENT, 0)

    async with session_factory() as session:
        skipped = await repository.skip_segment_manually(
            session, test_date, MANUAL_SKIP_SEGMENT,
            reason="Exchange declared no trades for this segment today",
            skipped_by="ops_user",
        )
        await session.commit()

    assert skipped is not None
    assert skipped.segment_status == SegmentStatus.SKIPPED
    assert skipped.skip_category == "MANUAL_SKIP"
    assert "ops_user" in skipped.skip_reason
    assert "Exchange declared no trades" in skipped.skip_reason

    # Drive the rest of the day to completion.
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert by_code[MANUAL_SKIP_SEGMENT].segment_status == SegmentStatus.SKIPPED
    assert by_code[MANUAL_SKIP_SEGMENT].skip_category == "MANUAL_SKIP"

    for code in SEGMENT_ORDER:
        if code != MANUAL_SKIP_SEGMENT:
            assert by_code[code].segment_status == SegmentStatus.COMPLETED, (
                f"segment {code} expected COMPLETED, unaffected by {MANUAL_SKIP_SEGMENT}'s "
                f"manual skip, got {by_code[code].segment_status}"
            )

    # The manually skipped segment's CBOS call count must never have grown
    # past what it was the instant it was skipped -- no further calls on any
    # later cycle, even though the rest of the day kept running.
    calls_after_full_day = cbos.call_counts.get(MANUAL_SKIP_SEGMENT, 0)
    assert calls_after_full_day == calls_before_skip, (
        f"{MANUAL_SKIP_SEGMENT}'s CBOS call count must freeze at manual-skip time "
        f"({calls_before_skip}), but grew to {calls_after_full_day} afterward"
    )


# =============================================================================
# Scenario 5 — End-of-day summary accuracy across a mixed COMPLETED/FAILED/
# SKIPPED outcome set.
# =============================================================================

END_OF_DAY_HOLIDAY_SEGMENTS = {"CUR"}
END_OF_DAY_FAIL_PAIRS = {"MCX": "BILLPOSTING"}


async def test_end_of_day_summary_matches_real_mixed_outcomes_exactly(cfg, session_factory, test_date):
    """
    A full day with all three terminal outcomes present at once (SKIPPED via
    holiday, FAILED via a CBOS pipeline error, COMPLETED for everything
    else) -- get_day_summary()'s aggregate counts must exactly match the
    real per-segment outcomes, with zero PENDING/IN_PROGRESS left over.
    """
    class MixedOutcomeCbosClient(CbosClient):
        def __init__(self, status_url: str, process_url: str):
            super().__init__(status_url, process_url, use_mock=True)

        async def file_process_status(self, segment: str, process_name: str, user_id: str) -> FileStatusResult:
            seg = segment.upper()
            if seg in END_OF_DAY_HOLIDAY_SEGMENTS and process_name == "BeginFileUpload":
                return FileStatusResult(response="SKIP", raw_body='{"Status":"SKIP"}', error=None, is_transient=False)
            if END_OF_DAY_FAIL_PAIRS.get(seg) == process_name:
                return FileStatusResult(
                    response="FALSE",
                    raw_body='{"Status":"CBOS_INTERNAL_ERROR"}',
                    error=f"Simulated permanent CBOS failure for {seg}/{process_name}",
                    is_transient=False,
                )
            return await super().file_process_status(segment, process_name, user_id)

    cbos = MixedOutcomeCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    expected_skipped = set(END_OF_DAY_HOLIDAY_SEGMENTS)
    expected_failed = set(END_OF_DAY_FAIL_PAIRS)
    expected_completed = set(SEGMENT_ORDER) - expected_skipped - expected_failed

    actual_skipped = {c for c in SEGMENT_ORDER if by_code[c].segment_status == SegmentStatus.SKIPPED}
    actual_failed = {c for c in SEGMENT_ORDER if by_code[c].segment_status == SegmentStatus.FAILED}
    actual_completed = {c for c in SEGMENT_ORDER if by_code[c].segment_status == SegmentStatus.COMPLETED}

    assert actual_skipped == expected_skipped
    assert actual_failed == expected_failed
    assert actual_completed == expected_completed

    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)

    assert summary["total"] == len(SEGMENT_ORDER)
    assert summary["completed"] == len(expected_completed)
    assert summary["failed"] == len(expected_failed)
    assert summary["skipped"] == len(expected_skipped)
    assert summary["pending"] == 0
    assert summary["in_progress"] == 0
    # Sanity: counts must exactly partition the day, no double-counting or
    # segment left out of the aggregate.
    assert (
        summary["completed"] + summary["failed"] + summary["skipped"]
        + summary["pending"] + summary["in_progress"]
        == summary["total"]
    )

    # Per-segment summary rows (embedded in get_day_summary) must also agree
    # with the DB rows' actual status/category exactly.
    summary_by_code = {s["segment_code"]: s for s in summary["segments"]}
    for code in SEGMENT_ORDER:
        assert summary_by_code[code]["segment_status"] == by_code[code].segment_status.value
