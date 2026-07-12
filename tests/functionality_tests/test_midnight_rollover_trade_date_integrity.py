"""
End-to-end midnight rollover coverage — a segment's `trade_date` (its
calendar-day identity in the DB) must never silently drift when the wall
clock crosses midnight mid-pipeline, in this 24/7 trading pipeline.

`trade_date` is set once, at row-creation time (repository.get_or_create()),
and is never recomputed from "now" afterwards — every later read (window
resolution, trigger recovery, CBOS calls) uses `row.trade_date`, not
`date.today()`. These tests prove that invariant holds across a real
midnight crossing, using the real orchestrator (`EdpOrchestrator.
run_wake_cycle()` / `_process_one_segment()` / `_process_one_post_trade()`)
against the real DB, monkeypatching only the wall clock
(`orchestrator.datetime` -> a fixed-`now()` stand-in), exactly at the point
identified in orchestrator.run_wake_cycle(): `now = datetime.now(self._tz)`.

Style follows tests/helpers.py's harness conventions (test_date/session_factory
fixtures, seed_day()/build_all_day_open_workflow_json(), _resolve_window()),
diverging only where a scenario needs a segment whose window genuinely
crosses midnight (NCDEX, configured 21:00->02:00) instead of the "wide open"
00:00-23:59 test workflow.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.agent.edp import repository
from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import SEGMENT_ORDER
from src.agent.edp.utils.datetime_utils import resolve_active_date
from src.tools.cbos_client import CbosClient

from .. import helpers

IST = ZoneInfo("Asia/Kolkata")
NCDEX_SEGMENT = "NCDEX"


def _midnight_crossing_workflow_json() -> dict:
    """NCDEX configured with a real overnight window (21:00 -> 02:00) so
    _resolve_window() genuinely rolls window_end onto trade_date+1 — every
    other segment stays wide-open so it never gates this test."""
    segments = []
    for code in SEGMENT_ORDER:
        if code == NCDEX_SEGMENT:
            segments.append({
                "segment_code": code, "login_id": "CV0001",
                "window_start": "21:00", "window_end": "02:00",
            })
        else:
            segments.append({
                "segment_code": code, "login_id": "CV0001",
                "window_start": "00:00", "window_end": "23:59",
            })
    return build_default_workflow_json(segments)


async def _seed_midnight_crossing_day(session_factory, trade_date: date) -> None:
    workflow_json = _midnight_crossing_workflow_json()
    async with session_factory() as session:
        await repository.upload(session, trade_date, workflow_json, uploaded_by="test")
        await session.commit()
    async with session_factory() as session:
        workflow = await repository.get_active(session, trade_date)
        await repository.seed_from_workflow(session, workflow, trade_date)
        await session.commit()


async def _get_row(session_factory, trade_date: date, segment_code: str):
    async with session_factory() as session:
        return await repository.get_one(session, trade_date, segment_code)


# ---------------------------------------------------------------------------
# Scenario 1: mid-pipeline segment crossing midnight — trade_date must never
# silently flip to T+1 while NCDEX's own row is still being driven.
# ---------------------------------------------------------------------------

async def test_segment_trade_date_stable_across_real_midnight_crossing(cfg, session_factory, test_date):
    """Drive NCDEX (real 21:00->02:00 overnight window) through several
    cycles as `now` crosses midnight. At every step row.trade_date must
    still equal test_date (T), never T+1, since trade_date is the row's
    fixed identity key, set once at creation."""
    trade_date = test_date
    await _seed_midnight_crossing_day(session_factory, trade_date)

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(0)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = trade_date

    checkpoints = [
        datetime.combine(trade_date, dtime(23, 58, 0), tzinfo=IST),
        datetime.combine(trade_date, dtime(23, 59, 59), tzinfo=IST),
        datetime.combine(trade_date + timedelta(days=1), dtime(0, 0, 0), tzinfo=IST),
        datetime.combine(trade_date + timedelta(days=1), dtime(0, 0, 1), tzinfo=IST),
        datetime.combine(trade_date + timedelta(days=1), dtime(0, 30, 0), tzinfo=IST),
    ]

    for now in checkpoints:
        orchestrator._cycle_now = now
        row = await _get_row(session_factory, trade_date, NCDEX_SEGMENT)
        if repository.is_handled(row):
            break
        await orchestrator._process_one_segment(NCDEX_SEGMENT)

        row = await _get_row(session_factory, trade_date, NCDEX_SEGMENT)
        assert row.trade_date == trade_date, (
            f"trade_date silently drifted at now={now.isoformat()}: "
            f"expected {trade_date}, got {row.trade_date}"
        )

    # Final sanity: the row (looked up BY trade_date=T) still exists and is
    # progressing/terminal under T, never re-keyed to T+1.
    row = await _get_row(session_factory, trade_date, NCDEX_SEGMENT)
    assert row is not None
    assert row.trade_date == trade_date
    # No stray row was ever created under trade_date+1 for this segment.
    ghost_row = await _get_row(session_factory, trade_date + timedelta(days=1), NCDEX_SEGMENT)
    assert ghost_row is None, "a segment row must not be duplicated onto trade_date+1 across midnight"


async def test_segment_reaches_completion_with_trade_date_intact_across_midnight(cfg, session_factory, test_date):
    """Driving NCDEX all the way to COMPLETED across a midnight boundary
    (repeatedly ticking `now` forward past 00:00) must never change which
    trade_date the row belongs to."""
    trade_date = test_date
    await _seed_midnight_crossing_day(session_factory, trade_date)

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(0)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = trade_date

    now = datetime.combine(trade_date, dtime(23, 59, 0), tzinfo=IST)
    max_cycles = 30
    for _ in range(max_cycles):
        orchestrator._cycle_now = now
        row = await _get_row(session_factory, trade_date, NCDEX_SEGMENT)
        if repository.is_handled(row):
            break
        await orchestrator._process_one_segment(NCDEX_SEGMENT)
        row = await _get_row(session_factory, trade_date, NCDEX_SEGMENT)
        assert row.trade_date == trade_date
        now = now + timedelta(minutes=1)
    else:
        pytest.fail(f"NCDEX segment did not reach terminal state within {max_cycles} cycles")

    row = await _get_row(session_factory, trade_date, NCDEX_SEGMENT)
    assert row.segment_status == SegmentStatus.COMPLETED
    assert row.trade_date == trade_date


# ---------------------------------------------------------------------------
# Scenario 2: resolve_active_date's cutoff, driven through a REAL
# run_wake_cycle() call — patched at the exact `now = datetime.now(self._tz)`
# call site in orchestrator.run_wake_cycle().
# ---------------------------------------------------------------------------

class _FixedDatetime(datetime):
    """A datetime subclass whose now()/now(tz) always returns a fixed
    instant — used to monkeypatch orchestrator.datetime so
    `datetime.now(self._tz)` inside run_wake_cycle() resolves to our chosen
    wall-clock instant, without touching any src/ file."""
    _fixed_now: datetime = None

    @classmethod
    def now(cls, tz=None):
        fixed = cls._fixed_now
        if tz is not None:
            return fixed.astimezone(tz)
        return fixed


def _patch_wall_clock(monkeypatch, fixed_now: datetime) -> None:
    import src.agent.edp.orchestrator as orchestrator_module

    _FixedDatetime._fixed_now = fixed_now
    monkeypatch.setattr(orchestrator_module, "datetime", _FixedDatetime)


async def _run_real_wake_cycle_at(cfg, monkeypatch, fixed_now: datetime) -> tuple[EdpOrchestrator, dict]:
    """Construct a fresh orchestrator and call the REAL run_wake_cycle()
    with the wall clock patched to fixed_now. default_segments is empty in
    `cfg`, so with no workflow uploaded for the resolved active_date the
    cycle safely no-ops after resolving active_date (see
    run_wake_cycle()'s "No workflow config and no defaults" branch) — we
    only need the resolved summary["active_date"], not a full pipeline run,
    for this scenario."""
    _patch_wall_clock(monkeypatch, fixed_now)
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)
    summary = await orchestrator.run_wake_cycle()
    return orchestrator, summary


@pytest.mark.parametrize(
    "hour, minute, expect_yesterday",
    [
        (4, 30, True),    # before cutoff (6) -> still processing yesterday's EDP
        (5, 59, True),    # 05:59:59 below -> still yesterday
        (6, 0, False),    # exactly at cutoff -> today
        (6, 1, False),    # just after cutoff -> today (uses 06:00:01 below)
    ],
)
async def test_run_wake_cycle_resolves_active_date_across_default_cutoff(
    cfg, session_factory, test_date, monkeypatch, hour, minute, expect_yesterday,
):
    """Real run_wake_cycle() call (not the raw resolve_active_date function)
    at each side of the default active_date_cutoff_hour=6 boundary. We pin
    "today" (in the fixed clock) to test_date so the resolved active_date
    lands on our isolated far-future date, never colliding with real
    trading data."""
    assert cfg.active_date_cutoff_hour == 6, "test assumes the default cutoff_hour=6"

    second = 59 if minute == 59 and hour == 5 else (1 if (hour, minute) == (6, 1) else 0)
    fixed_now = datetime.combine(test_date, dtime(hour, minute, second), tzinfo=IST)

    orchestrator, summary = await _run_real_wake_cycle_at(cfg, monkeypatch, fixed_now)

    expected_active_date = (test_date - timedelta(days=1)) if expect_yesterday else test_date
    assert summary["active_date"] == expected_active_date.isoformat()
    assert orchestrator._cycle_active_date == expected_active_date
    assert orchestrator._cycle_now == fixed_now

    # Cross-check against the pure function directly, for good measure.
    assert resolve_active_date(fixed_now, cfg.active_date_cutoff_hour, cfg.timezone) == expected_active_date


async def test_run_wake_cycle_exactly_at_040001_and_055959_boundaries(cfg, session_factory, test_date, monkeypatch):
    """Precise sub-minute boundary check: 05:59:59 IST resolves to
    yesterday, 06:00:00 IST (exactly at cutoff) resolves to today, and
    06:00:01 IST stays today — matches resolve_active_date()'s `local.hour
    < cutoff_hour` (minute/second-insensitive on the hour boundary)."""
    yesterday = test_date - timedelta(days=1)

    for hour, minute, second, expected in [
        (5, 59, 59, yesterday),
        (6, 0, 0, test_date),
        (6, 0, 1, test_date),
    ]:
        fixed_now = datetime.combine(test_date, dtime(hour, minute, second), tzinfo=IST)
        _, summary = await _run_real_wake_cycle_at(cfg, monkeypatch, fixed_now)
        assert summary["active_date"] == expected.isoformat(), (
            f"at {hour:02d}:{minute:02d}:{second:02d} IST expected active_date={expected}, "
            f"got {summary['active_date']}"
        )


# ---------------------------------------------------------------------------
# Scenario 3: TRIGGERED crash-safety across midnight — _recover_trigger()
# must operate against row.trade_date (persisted), never a recomputed
# "today".
# ---------------------------------------------------------------------------

SEGMENT_FOR_TRIGGER_TEST = "CUR"  # a plain segment, given an overnight window below


def _overnight_trigger_test_workflow_json() -> dict:
    """Same shape as helpers.build_all_day_open_workflow_json(), except
    SEGMENT_FOR_TRIGGER_TEST gets a real overnight window (17:00 -> 06:00)
    so a resume shortly after midnight is still within its window — this
    scenario is about TRIGGERED crash-recovery across midnight, not about
    the window-deadline check, so the window must genuinely span the
    boundary being tested."""
    segments = []
    for code in SEGMENT_ORDER:
        if code == SEGMENT_FOR_TRIGGER_TEST:
            segments.append({
                "segment_code": code, "login_id": "CV0001",
                "window_start": "17:00", "window_end": "06:00",
            })
        else:
            segments.append({
                "segment_code": code, "login_id": "CV0001",
                "window_start": "00:00", "window_end": "23:59",
            })
    return build_default_workflow_json(segments)


async def _seed_overnight_trigger_test_day(session_factory, trade_date: date) -> None:
    workflow_json = _overnight_trigger_test_workflow_json()
    async with session_factory() as session:
        await repository.upload(session, trade_date, workflow_json, uploaded_by="test")
        await session.commit()
    async with session_factory() as session:
        workflow = await repository.get_active(session, trade_date)
        await repository.seed_from_workflow(session, workflow, trade_date)
        await session.commit()


async def _prime_triggering_row(session_factory, orchestrator, trade_date: date, process_id: str, at: datetime) -> None:
    """Rewrite SEGMENT_FOR_TRIGGER_TEST's row into the exact state a crash
    would leave right after committing the pre-commit "TRIGGERING" marker
    (file upload + PID resolved, state=TRIGGERED), before the trigger call's
    outcome was ever recorded — mirrors
    test_trigger_double_trigger_protection.py's _seed_and_prime_triggering_row."""
    async with session_factory() as session:
        row = await repository.get_one(session, trade_date, SEGMENT_FOR_TRIGGER_TEST)
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = at
        row.process_id = process_id
        row.process_id_reserved_at = at
        row.current_state = SegmentState.TRIGGERED
        row.current_process = None
        row.processes_json = {
            SegmentState.INIT.value: {"status": "COMPLETED", "last_response": "TRUE"},
            SegmentState.WAITING_FOR_FILE_UPLOAD.value: {"status": "COMPLETED", "last_response": "TRUE"},
            SegmentState.TRIGGERED.value: {
                "status": "TRIGGERING",
                "attempt_started_at": at.isoformat(),
                "process_id_source": "RESERVED_NEW",
            },
        }
        await session.commit()


async def test_trigger_recovery_across_midnight_uses_persisted_trade_date_not_today(
    cfg, session_factory, test_date,
):
    """Segment enters TRIGGERED just before midnight on T (first trigger
    attempt commits the TRIGGERING marker). Agent then "resumes" after
    midnight (now=T+1 00:05) as if the pod restarted — _recover_trigger()
    must check CBOS/Table2 using row.trade_date == T (persisted), NOT
    whatever "today" would be if it (incorrectly) recomputed from `now`."""
    trade_date = test_date
    await _seed_overnight_trigger_test_day(session_factory, trade_date)

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    fake_pid = "77001"

    just_before_midnight = datetime.combine(trade_date, dtime(23, 59, 0), tzinfo=IST)
    await _prime_triggering_row(session_factory, orchestrator, trade_date, fake_pid, just_before_midnight)

    # Sanity: CBOS has never seen this (segment, trade_date) pair.
    trigger_key = (SEGMENT_FOR_TRIGGER_TEST, trade_date.isoformat())
    assert trigger_key not in cbos._mock_trigger_calls

    # "Resume after a pod restart" — now is past midnight, on trade_date+1,
    # but the row's own trade_date (T) is what must drive the recovery
    # check and everything downstream. active_date is intentionally left
    # at T here: a resumed IN_PROGRESS row is looked up/driven by its own
    # trade_date, exactly like drive_until_terminal()/run_one_cycle() do.
    resume_now = datetime.combine(trade_date + timedelta(days=1), dtime(0, 5, 0), tzinfo=IST)
    orchestrator._cycle_active_date = trade_date
    orchestrator._cycle_now = resume_now

    row_before = await _get_row(session_factory, trade_date, SEGMENT_FOR_TRIGGER_TEST)
    assert row_before.trade_date == trade_date
    assert row_before.processes_json[SegmentState.TRIGGERED.value]["status"] == "TRIGGERING"

    outcome = await orchestrator._process_one_segment(SEGMENT_FOR_TRIGGER_TEST)
    assert outcome == "advanced"

    # Table2 recovery worked: 2 CBOS calls (recovery check + the real
    # trigger, since CBOS never actually received the original call) keyed
    # by the ORIGINAL trade_date T, not T+1.
    assert cbos._mock_trigger_calls[trigger_key] == 2
    stale_key = (SEGMENT_FOR_TRIGGER_TEST, (trade_date + timedelta(days=1)).isoformat())
    assert stale_key not in cbos._mock_trigger_calls, (
        "recovery must key CBOS calls off the persisted row.trade_date, never a recomputed T+1 'today'"
    )

    row_after = await _get_row(session_factory, trade_date, SEGMENT_FOR_TRIGGER_TEST)
    assert row_after.trade_date == trade_date
    assert row_after.current_state == SegmentState.WAITING_FOR_BILLPOSTING
    assert row_after.processes_json[SegmentState.TRIGGERED.value]["status"] == "TRIGGERED"
    assert row_after.segment_status == SegmentStatus.IN_PROGRESS

    # Drive the remaining polls to completion manually, `now` anchored
    # within CUR's own (17:00 T -> 06:00 T+1) window — helpers.
    # drive_until_terminal()'s fixed_now_for() assumes noon-on-trade_date,
    # which falls outside this scenario's deliberately overnight window.
    for _ in range(10):
        row = await _get_row(session_factory, trade_date, SEGMENT_FOR_TRIGGER_TEST)
        if repository.is_handled(row):
            break
        orchestrator._cycle_now = resume_now
        await orchestrator._process_one_segment(SEGMENT_FOR_TRIGGER_TEST)
    row_final = await _get_row(session_factory, trade_date, SEGMENT_FOR_TRIGGER_TEST)
    assert row_final.segment_status == SegmentStatus.COMPLETED
    assert row_final.trade_date == trade_date


async def test_trigger_recovery_when_cbos_already_received_call_across_midnight(cfg, session_factory, test_date):
    """Crash AFTER CBOS received the trigger call but BEFORE the DB write
    of TRIGGERED completed, then resume past midnight. Recovery must NOT
    re-fire, and everything must stay keyed to the original trade_date T."""
    trade_date = test_date
    await _seed_overnight_trigger_test_day(session_factory, trade_date)

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    fake_pid = "77002"

    # Simulate the "lost" trigger call actually reaching CBOS before the crash.
    pre_call = await cbos.get_new_trade_process(
        group_name=SEGMENT_FOR_TRIGGER_TEST, login_id=cfg.cbos_login_id,
        trade_date=trade_date, process_id=fake_pid,
    )
    assert pre_call.success

    just_before_midnight = datetime.combine(trade_date, dtime(23, 59, 30), tzinfo=IST)
    await _prime_triggering_row(session_factory, orchestrator, trade_date, fake_pid, just_before_midnight)

    trigger_key = (SEGMENT_FOR_TRIGGER_TEST, trade_date.isoformat())
    assert cbos._mock_trigger_calls[trigger_key] == 1

    resume_now = datetime.combine(trade_date + timedelta(days=1), dtime(0, 5, 0), tzinfo=IST)
    orchestrator._cycle_active_date = trade_date
    orchestrator._cycle_now = resume_now

    outcome = await orchestrator._process_one_segment(SEGMENT_FOR_TRIGGER_TEST)
    assert outcome == "advanced"

    # 1 more call (the recovery check itself) — no re-trigger.
    assert cbos._mock_trigger_calls[trigger_key] == 2

    row_after = await _get_row(session_factory, trade_date, SEGMENT_FOR_TRIGGER_TEST)
    assert row_after.trade_date == trade_date
    assert row_after.current_state == SegmentState.WAITING_FOR_BILLPOSTING
    assert row_after.processes_json[SegmentState.TRIGGERED.value]["status"] == "TRIGGERED"


# ---------------------------------------------------------------------------
# Scenario 4: two consecutive real trade_dates processed "at once" in the
# same wake cycle pass — must never cross-contaminate.
# ---------------------------------------------------------------------------

async def test_two_consecutive_trade_dates_handled_independently_in_same_pass(cfg, session_factory, test_date):
    """Simulate the agent having been down across a midnight: T's NCDEX
    segment (mid-pipeline, wide-open window so it's still within deadline)
    is still incomplete, AND T+1's EQ segment (own window already open)
    should have started by the time the agent resumes at T+1 18:30. Both
    trade_date rows must be driven correctly and independently in the SAME
    wake-cycle-equivalent pass, with no cross-day interference or state
    merging — same segment_code (EQ) even exists under both trade_dates
    simultaneously, proving rows are keyed by (trade_date, segment_code),
    never by segment_code alone."""
    day_t = test_date
    day_t_plus_1 = test_date + timedelta(days=1)
    await helpers.cleanup_day(session_factory, day_t_plus_1)
    try:
        # Wide-open windows (helpers.seed_day's standard test workflow) for
        # BOTH days — this scenario is about cross-trade_date independence
        # in a single pass, not window/deadline timing (that's covered by
        # scenarios 1-3).
        await helpers.seed_day(session_factory, day_t, cfg)
        await helpers.seed_day(session_factory, day_t_plus_1, cfg)

        cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
        cbos.mock_set_ready_after(0)
        orchestrator = EdpOrchestrator(cfg, cbos)

        # T's NCDEX segment already started (mid-pipeline, e.g. polling
        # FILEUPLOAD) before the simulated outage began — models an
        # in-flight segment the agent stopped polling, not one that never
        # even started.
        in_flight_at = datetime.combine(day_t, dtime(21, 30, 0), tzinfo=IST)
        async with session_factory() as session:
            row = await repository.get_one(session, day_t, NCDEX_SEGMENT)
            row.segment_status = SegmentStatus.IN_PROGRESS
            row.started_at = in_flight_at
            row.current_state = SegmentState.INIT
            row.current_process = "BeginFileUpload"
            await session.commit()

        # Agent resumes late the SAME calendar day (T 23:30) — still just
        # before NCDEX's wide-open window deadline (T 23:59) so T's row can
        # keep progressing, while T+1's EQ segment (own window 00:00-23:59
        # on T+1, resolved independently against T+1) has NOT opened yet at
        # this instant. We therefore process T's segment at this instant,
        # then advance `now` again for T+1's pass below — modelling the
        # agent catching up across the boundary in two dated passes of the
        # SAME resumed run, exactly as run_wake_cycle() would (each
        # trade_date's own window is resolved independently against ITS
        # trade_date, never a shared wall-clock window).
        resume_now_t = datetime.combine(day_t, dtime(23, 30, 0), tzinfo=IST)
        resume_now_t_plus_1 = datetime.combine(day_t_plus_1, dtime(18, 30, 0), tzinfo=IST)

        # Drive T's NCDEX row (still incomplete) under its own trade_date.
        orchestrator._cycle_active_date = day_t
        orchestrator._cycle_now = resume_now_t
        for _ in range(30):
            row_t = await _get_row(session_factory, day_t, NCDEX_SEGMENT)
            if repository.is_handled(row_t):
                break
            await orchestrator._process_one_segment(NCDEX_SEGMENT)
        row_t = await _get_row(session_factory, day_t, NCDEX_SEGMENT)
        assert row_t.trade_date == day_t
        assert row_t.segment_status == SegmentStatus.COMPLETED

        # Drive T+1's EQ row under ITS trade_date, in the same test pass.
        orchestrator._cycle_active_date = day_t_plus_1
        orchestrator._cycle_now = resume_now_t_plus_1
        for _ in range(30):
            row_t1 = await _get_row(session_factory, day_t_plus_1, "EQ")
            if repository.is_handled(row_t1):
                break
            await orchestrator._process_one_segment("EQ")
        row_t1 = await _get_row(session_factory, day_t_plus_1, "EQ")
        assert row_t1.trade_date == day_t_plus_1
        assert row_t1.segment_status == SegmentStatus.COMPLETED

        # No cross-day interference: T's NCDEX row is unaffected by T+1's
        # EQ processing and vice versa; codes/trade_dates never merge.
        row_t_final = await _get_row(session_factory, day_t, NCDEX_SEGMENT)
        row_t1_final = await _get_row(session_factory, day_t_plus_1, "EQ")
        assert row_t_final.trade_date == day_t
        assert row_t1_final.trade_date == day_t_plus_1
        assert row_t_final.id != row_t1_final.id

        # T's EQ row (wide-open window, untouched in this test) still
        # belongs to T, distinct from T+1's EQ row — same segment_code,
        # different trade_date, never conflated.
        row_t_eq = await _get_row(session_factory, day_t, "EQ")
        assert row_t_eq is not None
        assert row_t_eq.trade_date == day_t
        assert row_t_eq.id != row_t1_final.id
    finally:
        await helpers.cleanup_day(session_factory, day_t_plus_1)


# ---------------------------------------------------------------------------
# Scenario 5: non-default active_date_cutoff_hour — cutoff shift changes
# which side of midnight resolves to "today".
# ---------------------------------------------------------------------------

async def test_non_default_cutoff_hour_shifts_active_date_resolution(cfg, session_factory, test_date, monkeypatch):
    """With active_date_cutoff_hour=4 (instead of the default 6),
    04:00 IST now resolves to TODAY instead of yesterday — proven via a
    real run_wake_cycle() call with cfg.active_date_cutoff_hour patched."""
    import dataclasses

    custom_cfg = dataclasses.replace(cfg, active_date_cutoff_hour=4)

    # 03:59:59 -> still before the new cutoff -> yesterday.
    fixed_now_before = datetime.combine(test_date, dtime(3, 59, 59), tzinfo=IST)
    _, summary_before = await _run_real_wake_cycle_at(custom_cfg, monkeypatch, fixed_now_before)
    assert summary_before["active_date"] == (test_date - timedelta(days=1)).isoformat()

    # 04:00:00 -> exactly at the NEW cutoff -> today (would have been
    # "yesterday" under the default cutoff_hour=6 — proves the shift).
    fixed_now_at_cutoff = datetime.combine(test_date, dtime(4, 0, 0), tzinfo=IST)
    _, summary_at = await _run_real_wake_cycle_at(custom_cfg, monkeypatch, fixed_now_at_cutoff)
    assert summary_at["active_date"] == test_date.isoformat()

    # Cross-check: under the ORIGINAL default cutoff_hour=6, that same
    # 04:00:00 instant would still resolve to yesterday — demonstrating the
    # cutoff_hour parameter genuinely drives the result.
    assert resolve_active_date(fixed_now_at_cutoff, cfg.active_date_cutoff_hour, cfg.timezone) == (
        test_date - timedelta(days=1)
    )
    assert resolve_active_date(fixed_now_at_cutoff, custom_cfg.active_date_cutoff_hour, custom_cfg.timezone) == (
        test_date
    )
