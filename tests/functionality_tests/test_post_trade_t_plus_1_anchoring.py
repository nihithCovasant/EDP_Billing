"""
End-to-end functionality tests for the post-trade pipeline's "always T+1,
regardless of configured times" invariant (see
orchestrator._resolve_post_trade_window() / _resolve_post_trade_window_end()):

    Post-trade processing is T+1 by definition — a config can override the
    gate *time*, but not that it falls on trade_date+1 ...

Both resolver functions unconditionally call
parse_window_dt(trade_date, <time>, next_day=True, tz) — there is no branch
anywhere that keeps a post-trade window on trade_date itself, no matter what
HH:MM string ops supplies. That's the invariant these tests try to break
with plausible ops misconfigurations, going further than
test_post_trade_config_driven.py (which covers config-driven login_id/
gtg_process_name/window resolution in general) and
test_post_trade_processes.py::test_post_trade_process1_window_gate (which
only exercises the *default* 02:30 window, not custom/adversarial ones).

Uses the real EdpOrchestrator, real DB, and helpers.py conventions exactly
as the rest of the suite — nothing here is mocked at the database layer.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

from src.agent.edp import repository
from src.agent.edp.api.workflow import _upload_workflow_for_date
from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import (
    EdpOrchestrator,
    _resolve_post_trade_window,
    _resolve_post_trade_window_end,
)
from src.tools.cbos_client import CbosClient

from .. import helpers


def _post_trade_workflow_json(post_trade_processes: list[dict]) -> dict:
    return build_default_workflow_json([], post_trade_processes=post_trade_processes)


async def _upload_and_seed_post_trade(session_factory, trade_date, post_trade_processes: list[dict]):
    workflow_json = _post_trade_workflow_json(post_trade_processes)
    async with session_factory() as session:
        await repository.upload(session, trade_date, workflow_json, uploaded_by="test")
        await session.commit()
    async with session_factory() as session:
        workflow = await repository.get_active(session, trade_date)
        await repository.seed_post_trade_processes(session, workflow, trade_date)
        await session.commit()


# ---------------------------------------------------------------------------
# Scenario 1 — a same-day-looking window_start ("14:00") must still resolve
# to trade_date+1 14:00, never trade_date 14:00.
# ---------------------------------------------------------------------------

async def test_same_day_looking_window_start_still_anchors_to_trade_date_plus_1(
    cfg, session_factory, test_date,
):
    """
    An ops person configuring COLVAL's window_start as "14:00" might
    plausibly mean "today at 2pm" — but _resolve_post_trade_window() always
    treats the HH:MM as trade_date+1 (next_day=True is hardcoded, never
    derived from the value itself, unlike _resolve_window()'s
    NEXT_DAY_WINDOW_SEGMENTS/rollover logic for real segments). Confirm the
    resolved window is trade_date+1 14:00 by construction, and confirm
    behaviourally: evaluating at trade_date 14:00 is blocked (window not
    open yet, because the true anchor is a full day later), while
    trade_date+1 14:00 proceeds.
    """
    # window_end is also pushed out (to "23:59") so the deadline check
    # doesn't confound this test — the point under test is purely the
    # OPENING gate's T+1 anchoring, not the closing deadline (that's
    # covered separately in scenario 2/6).
    await _upload_and_seed_post_trade(
        session_factory, test_date,
        [{"process_code": "COLVAL", "login_id": "G_LID", "window_start": "14:00", "window_end": "23:59"}],
    )

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
    window_start = _resolve_post_trade_window("COLVAL", workflow.workflow_json, test_date, orchestrator._tz)
    assert window_start.date() == test_date + timedelta(days=1), (
        "window_start must resolve on trade_date+1, never trade_date itself, "
        "regardless of the configured HH:MM value"
    )
    assert (window_start.hour, window_start.minute) == (14, 0)

    # Evaluate at trade_date 14:00 (same calendar day as the config value,
    # if someone misread it as "today") — must be blocked, PENDING.
    same_day_14 = datetime.combine(test_date, dtime(14, 0), tzinfo=orchestrator._tz)
    orchestrator._cycle_now = same_day_14
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "blocked", (
        "trade_date 14:00 must NOT open the window — the true anchor is trade_date+1"
    )
    rows = await helpers.get_post_trade_rows(session_factory, test_date)
    colval = next(r for r in rows if r.segment_code == "COLVAL")
    assert colval.segment_status == SegmentStatus.PENDING

    # Evaluate at trade_date+1 14:00 — the real anchor — must proceed.
    next_day_14 = datetime.combine(test_date + timedelta(days=1), dtime(14, 0), tzinfo=orchestrator._tz)
    orchestrator._cycle_now = next_day_14
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome in ("advanced", "completed"), (
        f"trade_date+1 14:00 is the real anchor and should proceed, got {outcome}"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — window_end BEFORE the default window_start (self-contradicting
# config). Tested empirically; no validation is assumed to exist.
# ---------------------------------------------------------------------------

async def test_window_end_before_default_window_start_makes_process_immediately_past_deadline(
    cfg, session_factory, test_date,
):
    """
    Ops configures window_end="02:00" for COLALLOC but leaves window_start
    unset (defaults to POST_TRADE_FIRST_WINDOW_START="02:30") — a
    self-contradicting config where the deadline is chronologically BEFORE
    the opening gate.

    There is no validation anywhere in _validate_workflow_json()
    (src/agent/edp/api/workflow.py) or _resolve_post_trade_window[_end]()
    that rejects window_end < window_start for post-trade processes (unlike
    _resolve_window() for real segments, which auto-rolls window_end
    forward a day when it's <= window_start on trade_date — see
    orchestrator.py lines ~504-505). Neither _resolve_post_trade_window()
    nor _resolve_post_trade_window_end() cross-checks the other's value at
    all; each independently anchors its own HH:MM to trade_date+1.

    Empirical result (this is what actually happens, not an assumption):
    window_start resolves to trade_date+1 02:30, window_end resolves to
    trade_date+1 02:00 — window_end is chronologically BEFORE window_start.
    The process can therefore never observe "window open AND deadline not
    yet passed" at any instant: the instant `now` reaches 02:30 (window
    opens per is_my_time_window()'s now >= window_start), is_my_window_over()
    (now > window_end, i.e. now > 02:00) is ALREADY true. The process is
    dead on arrival — PENDING at every instant before 02:30, then FAILED/
    TIMEOUT at the very first cycle evaluated at/after 02:30, with zero
    window in which it could actually run.
    """
    await _upload_and_seed_post_trade(
        session_factory, test_date,
        [{"process_code": "COLALLOC", "login_id": "G_LID", "window_end": "02:00"}],
    )

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
    window_start = _resolve_post_trade_window("COLALLOC", workflow.workflow_json, test_date, orchestrator._tz)
    window_end = _resolve_post_trade_window_end("COLALLOC", workflow.workflow_json, test_date, orchestrator._tz)

    # Both anchor to trade_date+1 (the T+1 invariant itself holds even in
    # this self-contradicting config) ...
    assert window_start.date() == test_date + timedelta(days=1)
    assert window_end.date() == test_date + timedelta(days=1)
    # ... but window_end is chronologically BEFORE window_start — no
    # cross-validation/auto-rollover exists for post-trade processes.
    assert window_end < window_start, (
        "empirically, window_end (02:00) resolves before window_start (02:30 default) "
        "with no correction — this is the self-contradicting state under test"
    )

    # Before 02:30 T+1: window not open yet -> blocked, PENDING (window gate
    # runs first and wins, since it's checked before the deadline check).
    before_open = datetime.combine(
        test_date + timedelta(days=1), dtime(2, 15), tzinfo=orchestrator._tz,
    )
    orchestrator._cycle_now = before_open
    outcome = await orchestrator._process_one_post_trade("COLALLOC")
    assert outcome == "blocked"
    rows = await helpers.get_post_trade_rows(session_factory, test_date)
    colalloc = next(r for r in rows if r.segment_code == "COLALLOC")
    assert colalloc.segment_status == SegmentStatus.PENDING

    # At/after 02:30 T+1: window is open (now >= window_start) AND the
    # deadline has already passed (now > window_end, since window_end is
    # earlier) -- the very first moment it could run, it's already too
    # late. Per _process_one_post_trade()'s ordering (orchestrator.py
    # ~390-419), the window-open check is evaluated first (True here), then
    # the deadline check (also True) fires and marks it FAILED/TIMEOUT
    # instead of ever starting. This is the self-contradicting config's
    # real, observed consequence -- COLALLOC can never run, ever, for this
    # trade_date.
    at_open = datetime.combine(
        test_date + timedelta(days=1), dtime(2, 30), tzinfo=orchestrator._tz,
    )
    orchestrator._cycle_now = at_open
    outcome = await orchestrator._process_one_post_trade("COLALLOC")
    assert outcome == "failed", (
        "COLALLOC must be immediately past-deadline the instant its window opens, "
        "given window_end < window_start with no cross-validation"
    )
    async with session_factory() as session:
        colalloc = await repository.get_one(session, test_date, "COLALLOC")
    assert colalloc.segment_status == SegmentStatus.FAILED
    assert colalloc.skip_category == "TIMEOUT"
    assert "deadline" in (colalloc.skip_reason or "").lower()


# ---------------------------------------------------------------------------
# Scenario 3 — mixed custom windows across processes in one upload; each
# process must resolve independently, with no cross-contamination.
# ---------------------------------------------------------------------------

async def test_mixed_custom_windows_resolve_independently_per_process(
    cfg, session_factory, test_date,
):
    """
    COLVAL window_start="01:00", DMSTMT window_start="05:00", DMRPT/MTFFT/
    COLALLOC left at default (02:30) -- one upload, one resolution pass per
    process. No process's custom time should leak into another's.
    """
    await _upload_and_seed_post_trade(
        session_factory, test_date,
        [
            {"process_code": "COLVAL", "login_id": "G_LID", "window_start": "01:00"},
            {"process_code": "COLALLOC", "login_id": "G_LID"},
            {"process_code": "MTFFT", "login_id": "G_LID"},
            {"process_code": "DMRPT", "login_id": "G_LID"},
            {"process_code": "DMSTMT", "login_id": "G_LID", "window_start": "05:00"},
        ],
    )

    orchestrator_tz_cfg = cfg
    orchestrator = EdpOrchestrator(
        orchestrator_tz_cfg, CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True),
    )
    tz = orchestrator._tz
    next_day = test_date + timedelta(days=1)

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)

    expected_starts = {
        "COLVAL": (1, 0),
        "COLALLOC": (2, 30),
        "MTFFT": (2, 30),
        "DMRPT": (2, 30),
        "DMSTMT": (5, 0),
    }
    for code, expected_hm in expected_starts.items():
        window_start = _resolve_post_trade_window(code, workflow.workflow_json, test_date, tz)
        assert window_start.date() == next_day, f"{code} window_start must anchor to trade_date+1"
        assert (window_start.hour, window_start.minute) == expected_hm, (
            f"{code} resolved window_start {window_start.time()} does not match expected "
            f"{expected_hm} -- possible cross-contamination from another process's config"
        )

    # And each process's window_end (all default here) is unaffected too.
    for code in ("COLVAL", "COLALLOC", "MTFFT", "DMRPT", "DMSTMT"):
        window_end = _resolve_post_trade_window_end(code, workflow.workflow_json, test_date, tz)
        assert window_end.date() == next_day
        assert (window_end.hour, window_end.minute) == (6, 0)


# ---------------------------------------------------------------------------
# Scenario 4 — DB-dependency gate (DMRPT waits on MTFFT) is independent of
# and ANDed with the time-window gate; an early custom window_start on DMRPT
# does not bypass waiting for MTFFT.
# ---------------------------------------------------------------------------

async def test_dmrpt_early_custom_window_does_not_bypass_mtfft_dependency_gate(
    cfg, session_factory, test_date,
):
    """
    DMRPT configured with window_start="00:30" (T+1) -- well before MTFFT
    would plausibly be terminal in a real run. Even though DMRPT's own
    window is open at 00:30 T+1 (way ahead of the other processes' default
    02:30), DMRPT must still sit BLOCKED on
    PostTradeStateMachine._check_previous_process_terminal() until MTFFT
    reaches a terminal DB status -- the window gate opening early does not
    let DMRPT skip the DB-dependency check.
    """
    await _upload_and_seed_post_trade(
        session_factory, test_date,
        [
            {"process_code": "COLVAL", "login_id": "G_LID"},
            {"process_code": "COLALLOC", "login_id": "G_LID"},
            {"process_code": "MTFFT", "login_id": "G_LID"},
            {"process_code": "DMRPT", "login_id": "G_LID", "window_start": "00:30"},
            {"process_code": "DMSTMT", "login_id": "G_LID"},
        ],
    )

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
    window_start = _resolve_post_trade_window("DMRPT", workflow.workflow_json, test_date, orchestrator._tz)
    assert window_start.date() == test_date + timedelta(days=1)
    assert (window_start.hour, window_start.minute) == (0, 30)

    # now is inside DMRPT's own (early, custom) window -- 01:00 T+1 -- but
    # MTFFT has not run at all yet (still PENDING, never even seeded/started
    # in this cycle since we only drive DMRPT directly).
    early_now = datetime.combine(
        test_date + timedelta(days=1), dtime(1, 0), tzinfo=orchestrator._tz,
    )
    orchestrator._cycle_now = early_now
    outcome = await orchestrator._process_one_post_trade("DMRPT")
    assert outcome == "blocked", (
        "DMRPT's time window is open at 01:00 T+1, but MTFFT hasn't reached a terminal "
        "state yet -- the DB-dependency gate must still block it"
    )

    async with session_factory() as session:
        dmrpt = await repository.get_one(session, test_date, "DMRPT")
    # DMRPT is IN_PROGRESS (its window opened, so it started and moved to
    # WAITING_FOR_GTG) but blocked inside that state waiting on MTFFT.
    assert dmrpt.segment_status == SegmentStatus.IN_PROGRESS
    assert dmrpt.current_state == SegmentState.WAITING_FOR_GTG

    # Now let MTFFT actually run to completion, then re-drive DMRPT at the
    # same early "now" -- it must now proceed, proving the DB gate (not the
    # window gate) was the only thing holding it back.
    orchestrator._cycle_now = helpers.fixed_post_trade_now_for(test_date, orchestrator._tz)
    mtfft_outcome = "blocked"
    for _ in range(20):
        mtfft_outcome = await orchestrator._process_one_post_trade("MTFFT")
        async with session_factory() as session:
            mtfft = await repository.get_one(session, test_date, "MTFFT")
        if repository.is_handled(mtfft):
            break
    assert mtfft.segment_status == SegmentStatus.COMPLETED

    orchestrator._cycle_now = early_now  # still DMRPT's own early window
    outcome = await orchestrator._process_one_post_trade("DMRPT")
    assert outcome in ("advanced", "completed"), (
        "once MTFFT is terminal, DMRPT must proceed -- its own window was already open"
    )


# ---------------------------------------------------------------------------
# Scenario 5 — mid-processing re-upload of a DIFFERENT post-trade window
# config defers to trade_date+1's chain, next trade_date -- must not
# retroactively alter the currently-active day's post-trade window.
# ---------------------------------------------------------------------------

async def test_post_trade_config_change_mid_chain_defers_to_next_trade_date(
    cfg, session_factory, test_date,
):
    """
    Following test_workflow_defer_midday.py's pattern but specifically for
    post_trade_processes / _resolve_post_trade_window() (a separate code
    path from _resolve_window() used by real segments) -- a re-upload with
    a different COLVAL window_start, landing while today's post-trade chain
    already has a process IN_PROGRESS, must be deferred to trade_date+1 and
    must NOT change what _resolve_post_trade_window() returns for the
    still-running trade_date.
    """
    next_day = test_date + timedelta(days=1)
    await helpers.cleanup_day(session_factory, next_day)
    try:
        await _upload_and_seed_post_trade(
            session_factory, test_date,
            [{"process_code": "COLVAL", "login_id": "G_LID", "window_start": "02:30"}],
        )

        # Simulate COLVAL already IN_PROGRESS for today's post-trade chain.
        async with session_factory() as session:
            row = await repository.get_one(session, test_date, "COLVAL")
            row.segment_status = SegmentStatus.IN_PROGRESS
            await session.commit()

        async with session_factory() as session:
            original_active = await repository.get_active(session, test_date)
        original_id = original_active.id

        new_workflow_json = _post_trade_workflow_json(
            [{"process_code": "COLVAL", "login_id": "G_LID", "window_start": "23:00"}],
        )
        resp = await _upload_workflow_for_date(test_date, new_workflow_json, "ops")

        assert resp["deferred"] is True
        assert resp["resolved_trade_date"] == test_date
        assert resp["trade_date"] == next_day, "deferred upload must land on trade_date + 1"

        # Today's active config -- and hence _resolve_post_trade_window()
        # for today -- must be completely untouched.
        async with session_factory() as session:
            still_active_today = await repository.get_active(session, test_date)
        assert still_active_today.id == original_id

        orchestrator = EdpOrchestrator(
            cfg, CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True),
        )
        window_start_today = _resolve_post_trade_window(
            "COLVAL", still_active_today.workflow_json, test_date, orchestrator._tz,
        )
        assert (window_start_today.hour, window_start_today.minute) == (2, 30), (
            "the deferred config's window_start (23:00) must NOT retroactively alter "
            "today's already-running post-trade window"
        )

        # The new config is waiting, active, for trade_date+1 instead.
        async with session_factory() as session:
            active_next_day = await repository.get_active(session, next_day)
        assert active_next_day is not None
        window_start_next_day = _resolve_post_trade_window(
            "COLVAL", active_next_day.workflow_json, next_day, orchestrator._tz,
        )
        assert (window_start_next_day.hour, window_start_next_day.minute) == (23, 0), (
            "the new config's custom window_start must apply once it takes effect on "
            "trade_date+1, resolving to (next_day + 1) 23:00 per the T+1 invariant"
        )
        assert window_start_next_day.date() == next_day + timedelta(days=1)
    finally:
        await helpers.cleanup_day(session_factory, next_day)


# ---------------------------------------------------------------------------
# Scenario 6 — boundary precision for CUSTOM window times: same inclusive/
# exclusive semantics as the default-window boundary tests
# (test_post_trade_processes.py), just exercised through a resolved custom
# time instead of the hardcoded 02:30/06:00 defaults.
# ---------------------------------------------------------------------------

async def test_custom_window_start_boundary_is_inclusive(cfg, session_factory, test_date):
    """
    is_my_time_window() uses `now >= window_start` (AbstractStateMachine.
    is_my_time_window) -- exactly AT the custom window_start instant, the
    process must already be considered open (inclusive), not one tick
    early/late. Uses a custom window_start ("04:15") instead of the
    hardcoded default, to prove the same boundary code path applies
    regardless of where the time value came from.
    """
    await _upload_and_seed_post_trade(
        session_factory, test_date,
        [{"process_code": "COLVAL", "login_id": "G_LID", "window_start": "04:15"}],
    )

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    next_day = test_date + timedelta(days=1)

    # One second before the custom window_start -- must still be blocked.
    just_before = datetime.combine(next_day, dtime(4, 14, 59), tzinfo=orchestrator._tz)
    orchestrator._cycle_now = just_before
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "blocked"
    rows = await helpers.get_post_trade_rows(session_factory, test_date)
    colval = next(r for r in rows if r.segment_code == "COLVAL")
    assert colval.segment_status == SegmentStatus.PENDING

    # Exactly at the custom window_start -- must be open (inclusive).
    exactly_at = datetime.combine(next_day, dtime(4, 15, 0), tzinfo=orchestrator._tz)
    orchestrator._cycle_now = exactly_at
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome in ("advanced", "completed"), (
        "at exactly window_start, the process must already be open (now >= window_start)"
    )


async def test_custom_window_end_boundary_is_exclusive_for_over_check(cfg, session_factory, test_date):
    """
    is_my_window_over() uses `now > window_end` (strictly greater) -- AT the
    custom window_end instant itself, the deadline must NOT yet be
    considered passed; only strictly after it should the PENDING-past-
    deadline FAILED/TIMEOUT check fire. Uses a custom window_end ("04:45")
    instead of the hardcoded default 06:00.
    """
    await _upload_and_seed_post_trade(
        session_factory, test_date,
        [{"process_code": "COLVAL", "login_id": "G_LID", "window_end": "04:45"}],
    )

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    next_day = test_date + timedelta(days=1)

    # Exactly at window_end -- deadline not yet "over" (strict >), so a
    # still-PENDING row must NOT be failed at this exact instant. (Window
    # is open too, since default window_start 02:30 <= 04:45.)
    exactly_at_end = datetime.combine(next_day, dtime(4, 45, 0), tzinfo=orchestrator._tz)
    orchestrator._cycle_now = exactly_at_end
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome != "failed", (
        "exactly at window_end, now > window_end is False -- must not be treated as "
        "past-deadline yet"
    )

    # Reset back to PENDING to isolate the deadline check at the next
    # instant (the previous call may have started the process).
    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "COLVAL")
        row.segment_status = SegmentStatus.PENDING
        row.current_state = None
        row.current_process = None
        row.started_at = None
        row.processes_json = {}
        await session.commit()

    # One second after window_end -- now the deadline check must fire.
    just_after_end = datetime.combine(next_day, dtime(4, 45, 1), tzinfo=orchestrator._tz)
    orchestrator._cycle_now = just_after_end
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "failed", "one second past window_end, a still-PENDING row must FAIL/TIMEOUT"

    async with session_factory() as session:
        colval = await repository.get_one(session, test_date, "COLVAL")
    assert colval.segment_status == SegmentStatus.FAILED
    assert colval.skip_category == "TIMEOUT"
