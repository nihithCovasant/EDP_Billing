"""
End-to-end segment-timing-window tests against the REAL, documented
per-segment business hours (IST) — as opposed to test_window_resolution.py
(pure _resolve_window() unit checks) and helpers.seed_day()'s "wide open
00:00-23:59" default (which deliberately never exercises any real gating).

Real documented windows (confirmed with the business):
    CASH (EQ):    17:00-18:00, same day
    DR (F&O):     18:00-21:00, same day
    Currencies:   18:00-19:00, same day
    SLBM (SLB):   18:10-19:00, same day
    NCDEX:        21:00-02:00, spans midnight into T+1
    NCDEXPHY:     same window as NCDEX
    MCX:          04:00-06:00, entirely on T+1 (NEXT_DAY_WINDOW_SEGMENTS)
    MCXPHY:       same window as MCX
    NSECOM:       03:30-06:00, entirely on T+1 (NEXT_DAY_WINDOW_SEGMENTS)

All scenarios drive the REAL EdpOrchestrator (orchestrator._process_one_segment)
against a real, uniquely-isolated far-future trade_date, per tests/helpers.py's
harness conventions — never calling _resolve_window()/is_my_time_window()/
is_my_window_over() directly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dtime

from src.agent.edp import repository
from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.models import SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient

from .. import helpers
from ..fakes import RecordingFileStatusCbosClient

# Real documented per-segment windows (HH:MM strings, as workflow_json stores them).
REAL_WINDOWS: dict[str, tuple[str, str]] = {
    "EQ": ("17:00", "18:00"),
    "DR": ("18:00", "21:00"),
    "CUR": ("18:00", "19:00"),
    "SLB": ("18:10", "19:00"),
    "NCDEX": ("21:00", "02:00"),
    "NCDEXPHY": ("21:00", "02:00"),
    "MCX": ("04:00", "06:00"),
    "MCXPHY": ("04:00", "06:00"),
    "NSECOM": ("03:30", "06:00"),
}


def _build_real_windows_workflow_json(segment_codes: list[str]) -> dict:
    """
    Build a workflow_json using the REAL documented per-segment windows
    (see REAL_WINDOWS above) for exactly the given segments — closer to how
    ops would actually configure the live system than helpers.seed_day()'s
    "wide open" test default.
    """
    segments = []
    for code in segment_codes:
        window_start, window_end = REAL_WINDOWS[code]
        segments.append(
            {
                "segment_code": code,
                "login_id": "CV0001",
                "window_start": window_start,
                "window_end": window_end,
            }
        )
    return build_default_workflow_json(segments)


async def _seed_real_windows_day(
    session_factory,
    trade_date: date,
    segment_codes: list[str],
) -> None:
    """Upload a workflow_json with the REAL documented windows for exactly
    these segments, then seed their segment_execution rows — the real-windows
    equivalent of helpers.seed_day()."""
    workflow_json = _build_real_windows_workflow_json(segment_codes)
    async with session_factory() as session:
        await repository.upload(session, trade_date, workflow_json, uploaded_by="test")
        await session.commit()

    async with session_factory() as session:
        workflow = await repository.get_active(session, trade_date)
        await repository.seed_from_workflow(session, workflow, trade_date)
        await session.commit()


def _at(trade_date: date, hh: int, mm: int, tz, ss: int = 0, us: int = 0, day_offset: int = 0) -> datetime:
    """A tz-aware datetime at trade_date+day_offset, HH:MM:SS.ffffff."""
    return datetime.combine(
        trade_date + timedelta(days=day_offset),
        dtime(hh, mm, ss, us),
        tzinfo=tz,
    )


async def _get_row(session_factory, trade_date: date, segment_code: str):
    async with session_factory() as session:
        return await repository.get_one(session, trade_date, segment_code)


# =============================================================================
# 1. Before-window-opens: DR polled at 17:30, before its 18:00 open.
# =============================================================================


async def test_before_window_opens_dr_blocked_and_zero_cbos_calls(cfg, session_factory, test_date):
    """
    DR's real window is 18:00-21:00. Polling at 17:30 (before open) must
    return "blocked", leave the row PENDING, and — critically — make ZERO
    CBOS calls. Polling CBOS outside the allowed window would be a real
    cost/correctness bug even if state itself isn't corrupted.
    """
    await _seed_real_windows_day(session_factory, test_date, ["DR"])

    counting_cbos = RecordingFileStatusCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    orchestrator = EdpOrchestrator(cfg, counting_cbos)
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = _at(test_date, 17, 30, orchestrator._tz)

    outcome = await orchestrator._process_one_segment("DR")

    assert outcome == "blocked"
    row = await _get_row(session_factory, test_date, "DR")
    assert row.segment_status == SegmentStatus.PENDING
    assert row.started_at is None
    assert counting_cbos.calls == [], f"expected zero CBOS calls before DR's window opens, got {counting_cbos.calls}"


# =============================================================================
# 2. Exactly-at-window-open: DR polled at exactly 18:00:00.000000.
# =============================================================================


async def test_exactly_at_window_open_dr_proceeds(cfg, session_factory, test_date):
    """DR's window opens at 18:00 — is_my_time_window() is an inclusive (>=)
    check, so a poll at exactly 18:00:00.000000 must proceed (not "blocked")."""
    await _seed_real_windows_day(session_factory, test_date, ["DR"])

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = _at(test_date, 18, 0, orchestrator._tz, ss=0, us=0)

    outcome = await orchestrator._process_one_segment("DR")

    assert outcome != "blocked", "DR must be allowed to start at exactly its window_start"
    row = await _get_row(session_factory, test_date, "DR")
    assert row.segment_status == SegmentStatus.IN_PROGRESS
    assert row.started_at is not None


# =============================================================================
# 3. Mid-window normal operation: Currencies (CUR) at 18:30.
# =============================================================================


async def test_mid_window_currencies_normal_progress(cfg, session_factory, test_date):
    """CUR's window is 18:00-19:00. Driven entirely within its own real
    window (repeated polls at 18:30) it must complete normally."""
    await _seed_real_windows_day(session_factory, test_date, ["CUR"])

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    fixed_now = _at(test_date, 18, 30, orchestrator._tz)
    for _ in range(20):
        row = await _get_row(session_factory, test_date, "CUR")
        if repository.is_handled(row):
            break
        orchestrator._cycle_now = fixed_now
        await orchestrator._process_one_segment("CUR")
    else:
        raise AssertionError("CUR did not reach a terminal state within 20 cycles at 18:30")

    row = await _get_row(session_factory, test_date, "CUR")
    assert row.segment_status == SegmentStatus.COMPLETED


# =============================================================================
# 4. After-window-closes without ever starting: SLB polled at 19:30 (after
#    its 19:00 close), never having started -> FAILED/TIMEOUT.
# =============================================================================


async def test_after_window_closes_never_started_slb_fails_timeout(cfg, session_factory, test_date):
    """SLB's window is 18:10-19:00. If it's still PENDING by 19:30 (past
    close, never got a chance to start), the orchestrator must fail it as a
    local TIMEOUT, not silently leave it PENDING or SKIP it."""
    await _seed_real_windows_day(session_factory, test_date, ["SLB"])

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = _at(test_date, 19, 30, orchestrator._tz)

    outcome = await orchestrator._process_one_segment("SLB")

    assert outcome == "failed"
    row = await _get_row(session_factory, test_date, "SLB")
    assert row.segment_status == SegmentStatus.FAILED
    assert row.skip_category == "TIMEOUT"


# =============================================================================
# 5. Midnight-spanning window: NCDEX (21:00-02:00) driven across T -> T+1.
# =============================================================================


async def test_midnight_spanning_ncdex_stays_open_across_calendar_boundary(cfg, session_factory, test_date):
    """
    NCDEX's window is 21:00 (T) -> 02:00 (T+1). Start it at 21:30 T, then
    keep polling it at 23:59 T and 00:30 T+1 — it must remain treated as
    open the whole time (no reset/close at the calendar T/T+1 boundary,
    only at the real 02:00 window_end), and the row's trade_date must stay
    pinned to T throughout — never silently drift to T+1.
    """
    await _seed_real_windows_day(session_factory, test_date, ["NCDEX"])

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    # Never ready -> segment stays IN_PROGRESS/BLOCKED for the whole test;
    # we only care that the window itself stays open, not that it completes.
    cbos.mock_set_ready_after(10_000)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    # Start at 21:30 on trade_date (T). Note: with mock_set_ready_after(10_000)
    # the outcome string is "blocked" both when the WINDOW isn't open yet and
    # when the window IS open but CBOS just hasn't returned TRUE yet — so we
    # assert on row.segment_status (PENDING -> IN_PROGRESS only happens once
    # the window gate itself lets the row through) rather than the outcome string.
    orchestrator._cycle_now = _at(test_date, 21, 30, orchestrator._tz)
    await orchestrator._process_one_segment("NCDEX")
    row = await _get_row(session_factory, test_date, "NCDEX")
    assert row.segment_status == SegmentStatus.IN_PROGRESS, (
        "NCDEX's window (21:00-02:00) is open at 21:30 T — the row must have "
        "left PENDING even though CBOS itself hasn't confirmed BeginFileUpload yet"
    )
    assert row.trade_date == test_date

    # Poll again at 23:59 T — still open, still pinned to T.
    orchestrator._cycle_now = _at(test_date, 23, 59, orchestrator._tz)
    await orchestrator._process_one_segment("NCDEX")
    row = await _get_row(session_factory, test_date, "NCDEX")
    assert row.segment_status == SegmentStatus.IN_PROGRESS, (
        "NCDEX must still be open (not FAILED/TIMEOUT) at 23:59 T, well before its 02:00 T+1 close"
    )
    assert row.trade_date == test_date, "trade_date must not drift once midnight passes"

    # Poll again at 00:30 on the T+1 calendar day — still open, still pinned to T.
    orchestrator._cycle_now = _at(test_date, 0, 30, orchestrator._tz, day_offset=1)
    await orchestrator._process_one_segment("NCDEX")
    row = await _get_row(session_factory, test_date, "NCDEX")
    assert row.segment_status == SegmentStatus.IN_PROGRESS, (
        "NCDEX must still be treated as open (not FAILED/TIMEOUT) at 00:30 on the "
        "T+1 calendar day — the window doesn't close at the calendar boundary, "
        "only at the actual 02:00 window_end"
    )
    assert row.trade_date == test_date, (
        "the segment_execution row's trade_date must stay pinned to T even after "
        "the wall-clock calendar day has rolled over to T+1"
    )


# =============================================================================
# 6. NCDEX exactly at its window_end (02:00 T+1): not yet over (inclusive of
#    the deadline instant); one instant past -> FAILED/TIMEOUT if incomplete.
# =============================================================================


async def test_ncdex_exactly_at_deadline_not_yet_over_then_fails_one_instant_later(cfg, session_factory, test_date):
    """is_my_window_over() uses a strict `>` — exactly at window_end (02:00
    T+1) the window must NOT yet be considered over; one microsecond past it,
    with the row still incomplete, it must fail as TIMEOUT."""
    await _seed_real_windows_day(session_factory, test_date, ["NCDEX"])

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(10_000)  # never completes on its own
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    # Get it started well inside the window first.
    orchestrator._cycle_now = _at(test_date, 21, 30, orchestrator._tz)
    await orchestrator._process_one_segment("NCDEX")
    row = await _get_row(session_factory, test_date, "NCDEX")
    assert row.segment_status == SegmentStatus.IN_PROGRESS

    # Exactly at window_end (02:00 T+1) — still IN_PROGRESS (not FAILED yet).
    # Note: the window-deadline-without-having-started FAILED branch only
    # triggers for PENDING rows; an IN_PROGRESS row just keeps polling CBOS
    # (BLOCKED outcome) whether or not the deadline is "over", so we assert
    # on status/outcome rather than a raw is_my_window_over() call.
    orchestrator._cycle_now = _at(test_date, 2, 0, orchestrator._tz, ss=0, us=0, day_offset=1)
    outcome = await orchestrator._process_one_segment("NCDEX")
    row = await _get_row(session_factory, test_date, "NCDEX")
    assert row.segment_status == SegmentStatus.IN_PROGRESS, (
        "at exactly window_end (02:00 T+1), the window must not yet be considered "
        "over (strict '>' design) — the segment should still be actively processed"
    )

    # Now test the PENDING-past-deadline TIMEOUT path directly, matching
    # scenario 4's shape: a fresh NCDEX row still PENDING once the deadline
    # has strictly passed must fail as TIMEOUT.
    await helpers.cleanup_day(session_factory, test_date)
    await _seed_real_windows_day(session_factory, test_date, ["NCDEX"])
    orchestrator._cycle_now = _at(test_date, 2, 0, orchestrator._tz, ss=0, us=1, day_offset=1)
    outcome = await orchestrator._process_one_segment("NCDEX")
    assert outcome == "failed"
    row = await _get_row(session_factory, test_date, "NCDEX")
    assert row.segment_status == SegmentStatus.FAILED
    assert row.skip_category == "TIMEOUT"


# =============================================================================
# 7. T+1-only window: MCX (04:00-06:00, entirely T+1) always "blocked" on T
#    itself, even at 23:59 T, then opens correctly at 04:00 T+1.
# =============================================================================


async def test_mcx_next_day_window_never_opens_on_trade_date_itself(cfg, session_factory, test_date):
    """
    MCX is a NEXT_DAY_WINDOW_SEGMENTS member — its entire window (04:00-06:00)
    falls on trade_date+1, never trade_date itself. Polling MCX at any time
    on T (including 23:59 T) must always be "blocked"; it should only open
    once "now" reaches 04:00 on T+1.
    """
    await _seed_real_windows_day(session_factory, test_date, ["MCX"])

    counting_cbos = RecordingFileStatusCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    counting_cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, counting_cbos)
    orchestrator._cycle_active_date = test_date

    for hh, mm in [(0, 0), (4, 0), (12, 0), (20, 0), (23, 59)]:
        orchestrator._cycle_now = _at(test_date, hh, mm, orchestrator._tz)
        outcome = await orchestrator._process_one_segment("MCX")
        assert outcome == "blocked", (
            f"MCX must be blocked at {hh:02d}:{mm:02d} on trade_date itself — "
            f"its whole window is T+1, got outcome={outcome!r}"
        )

    row = await _get_row(session_factory, test_date, "MCX")
    assert row.segment_status == SegmentStatus.PENDING
    assert counting_cbos.calls == [], "no CBOS calls should occur while MCX's T+1 window hasn't opened"

    # Now advance to 04:00 on T+1 — MCX must open correctly.
    orchestrator._cycle_now = _at(test_date, 4, 0, orchestrator._tz, day_offset=1)
    outcome = await orchestrator._process_one_segment("MCX")
    assert outcome != "blocked", "MCX must open at exactly 04:00 on T+1"
    row = await _get_row(session_factory, test_date, "MCX")
    assert row.segment_status == SegmentStatus.IN_PROGRESS


# =============================================================================
# 8. Back-to-back adjacent windows, no gap/overlap bug: DR closes at 21:00
#    the same instant NCDEX opens at 21:00.
# =============================================================================


async def test_dr_close_and_ncdex_open_handoff_at_21_00_no_dead_or_double_zone(cfg, session_factory, test_date):
    """
    DR's window ends at 21:00; NCDEX's window starts at 21:00 — same instant.

    is_my_window_over() is a strict `>` check (see test_window_resolution.py's
    docstring reference and AbstractStateMachine.is_my_window_over): AT exactly
    21:00:00.000000, DR's deadline is NOT yet "over" (inclusive-of-deadline
    design) — so a still-PENDING DR is correctly let through to start this
    cycle rather than timed out one instant early. NCDEX, whose window_start is
    also exactly 21:00, is simultaneously open (is_my_time_window is a `>=`
    check). Both sides being active at the exact handoff instant is the
    CORRECT behavior — no dead zone (neither open) would be the actual bug;
    this proves the design leans "no dead zone" rather than "double timeout".
    """
    await _seed_real_windows_day(session_factory, test_date, ["DR", "NCDEX"])

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = _at(test_date, 21, 0, orchestrator._tz, ss=0, us=0)

    dr_outcome = await orchestrator._process_one_segment("DR")
    ncdex_outcome = await orchestrator._process_one_segment("NCDEX")

    dr_row = await _get_row(session_factory, test_date, "DR")
    ncdex_row = await _get_row(session_factory, test_date, "NCDEX")

    # DR: exactly at its deadline instant, NOT yet timed out -> allowed to
    # start this cycle (proceeds into IN_PROGRESS), not FAILED.
    assert dr_outcome != "blocked"
    assert dr_row.segment_status == SegmentStatus.IN_PROGRESS, (
        "at exactly 21:00:00.000000 DR's window_end, is_my_window_over()'s "
        "strict '>' means DR must NOT be timed out yet — no dead zone"
    )

    # NCDEX: simultaneously open at its window_start instant.
    assert ncdex_outcome != "blocked", (
        "NCDEX must be simultaneously open at exactly 21:00:00.000000 — the handoff instant must not create a dead zone"
    )
    assert ncdex_row.segment_status == SegmentStatus.IN_PROGRESS

    # One microsecond later, DR (if still incomplete) DOES cross into TIMEOUT
    # — confirming the boundary is a real edge, not "always let it through".
    await helpers.cleanup_day(session_factory, test_date)
    await _seed_real_windows_day(session_factory, test_date, ["DR"])
    orchestrator._cycle_now = _at(test_date, 21, 0, orchestrator._tz, ss=0, us=1)
    dr_outcome_late = await orchestrator._process_one_segment("DR")
    dr_row_late = await _get_row(session_factory, test_date, "DR")
    assert dr_outcome_late == "failed"
    assert dr_row_late.segment_status == SegmentStatus.FAILED
    assert dr_row_late.skip_category == "TIMEOUT"


# =============================================================================
# 9. Multiple overlapping segments in ONE wake cycle: independence check.
#    At 18:15, DR/CUR/SLB are all open, EQ has just closed. Confirm each
#    segment's outcome depends ONLY on its own window/state.
# =============================================================================


async def test_overlapping_segments_at_18_15_independent_outcomes(cfg, session_factory, test_date):
    """
    Realistic overlapping-windows snapshot at 18:15 IST:
      EQ  (17:00-18:00) has just closed, never started -> FAILED/TIMEOUT.
      DR  (18:00-21:00) is open -> proceeds normally.
      CUR (18:00-19:00) is open, but its one CBOS call is rigged to fail
          permanently -> FAILED (pipeline failure), independent of the others.
      SLB (18:10-19:00) is open -> proceeds normally, unaffected by CUR's
          failure or EQ's timeout.
    Confirms zero cross-contamination between segments processed in the same cycle.
    """
    await _seed_real_windows_day(session_factory, test_date, ["EQ", "DR", "CUR", "SLB"])

    from ..fakes import FailingCbosClient

    cbos = FailingCbosClient(
        cfg.cbos_status_url,
        cfg.cbos_process_url,
        fail_segment="CUR",
        fail_process="BeginFileUpload",
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = _at(test_date, 18, 15, orchestrator._tz)

    outcomes = {}
    for code in ["EQ", "DR", "CUR", "SLB"]:
        outcomes[code] = await orchestrator._process_one_segment(code)

    eq_row = await _get_row(session_factory, test_date, "EQ")
    dr_row = await _get_row(session_factory, test_date, "DR")
    cur_row = await _get_row(session_factory, test_date, "CUR")
    slb_row = await _get_row(session_factory, test_date, "SLB")

    # EQ: window closed at 18:00, never started -> TIMEOUT.
    assert eq_row.segment_status == SegmentStatus.FAILED
    assert eq_row.skip_category == "TIMEOUT"

    # DR: open window, healthy CBOS path -> actively progressing.
    assert dr_row.segment_status == SegmentStatus.IN_PROGRESS
    assert outcomes["DR"] != "blocked"

    # CUR: open window, but its CBOS call is rigged to fail permanently.
    # Drive CUR the rest of the way to confirm it lands FAILED for its OWN
    # pipeline reason, not lumped in with EQ's TIMEOUT category.
    for _ in range(10):
        row = await _get_row(session_factory, test_date, "CUR")
        if repository.is_handled(row):
            break
        await orchestrator._process_one_segment("CUR")
    cur_row = await _get_row(session_factory, test_date, "CUR")
    assert cur_row.segment_status == SegmentStatus.FAILED
    assert cur_row.skip_category != "TIMEOUT", (
        "CUR's failure must be attributed to its own CBOS error, not conflated with EQ's window-timeout category"
    )

    # SLB: open window, healthy CBOS path -> must be completely unaffected by
    # CUR's failure or EQ's timeout — drive it to completion independently.
    for _ in range(20):
        row = await _get_row(session_factory, test_date, "SLB")
        if repository.is_handled(row):
            break
        orchestrator._cycle_now = _at(test_date, 18, 15, orchestrator._tz)
        await orchestrator._process_one_segment("SLB")
    slb_row = await _get_row(session_factory, test_date, "SLB")
    assert slb_row.segment_status == SegmentStatus.COMPLETED, (
        "SLB must complete normally — CUR's rigged CBOS failure and EQ's "
        "timeout must have zero cross-contamination on SLB's own outcome"
    )
