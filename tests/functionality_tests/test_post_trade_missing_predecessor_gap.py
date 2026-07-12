"""
Bug hunt: PostTradeStateMachine._check_previous_process_terminal() is the
DB-based readiness gate for DMRPT (waits for MTFFT) and DMSTMT (waits for
DMRPT).

Suspected gap: if the predecessor row was never seeded at all (e.g.
MTFFT's row doesn't exist for that trade_date), DMRPT's gate returns
BLOCKED every cycle forever. The PENDING-only window-deadline FAILED/
TIMEOUT safety net in orchestrator._process_one_post_trade() can't catch
this: DMRPT moves PENDING -> IN_PROGRESS in the very cycle it's seeded
(before the gate ever runs), so the PENDING-only deadline guard never
fires again, and there's no other timeout logic in this gate.

This test seeds only DMRPT and DMSTMT for a trade_date, deletes MTFFT's
row entirely, then drives many wake-cycle-equivalent passes — including
passes well past the post-trade window's default deadline — and asserts
on DMRPT's final segment_status.
"""

from __future__ import annotations

from datetime import time as dtime, timedelta

from sqlalchemy import delete

from src.agent.edp import repository
from src.agent.edp.models import SegmentExecution, SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient

from .. import helpers


async def test_dmrpt_stuck_forever_when_predecessor_row_never_seeded(cfg, session_factory, test_date):
    """
    Seed only DMRPT + DMSTMT (skip MTFFT entirely), then drive 30
    wake-cycle-equivalent passes with `now` advancing well past the
    default post-trade window deadline (06:00 IST trade_date+1).

    If the suspected bug is real: DMRPT never reaches a terminal state
    (stays PENDING/IN_PROGRESS/BLOCKED-forever) even after we simulate time
    far past the deadline -- because the window-deadline FAILED/TIMEOUT
    check in orchestrator._process_one_post_trade() only fires while
    row.segment_status == SegmentStatus.PENDING, and DMRPT flips to
    IN_PROGRESS on its very first cycle (before _check_previous_process_terminal
    ever runs), so the deadline branch can never be reached again.

    If the bug is NOT present: DMRPT will show FAILED/TIMEOUT (or some
    other terminal state) once `now` passes the deadline.
    """
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    # Seed all 5 post-trade rows normally, then explicitly delete MTFFT's
    # row so it never existed for this trade_date -- this is the cleanest
    # way to get DMRPT + DMSTMT seeded without MTFFT, since
    # seed_post_trade_day() seeds all 5 in one shot via
    # repository.seed_post_trade_processes().
    await helpers.seed_post_trade_day(session_factory, test_date)
    async with session_factory() as session:
        await session.execute(
            delete(SegmentExecution).where(
                SegmentExecution.trade_date == test_date,
                SegmentExecution.segment_code == "MTFFT",
            )
        )
        await session.commit()

    async with session_factory() as session:
        remaining = await repository.get_all_for_date(session, test_date)
    codes_present = {r.segment_code for r in remaining}
    assert "MTFFT" not in codes_present
    assert "DMRPT" in codes_present
    assert "DMSTMT" in codes_present

    tz = orchestrator._tz
    # Post-trade window: opens 02:30 IST trade_date+1, default deadline
    # 06:00 IST trade_date+1 (POST_TRADE_DEFAULT_WINDOW_END). Drive cycles
    # from inside the window, then well past the deadline, then even
    # further out (next-day-equivalent) to rule out "just needs more time".
    base_day = test_date + timedelta(days=1)
    from datetime import datetime as _dt
    now_points = [
        _dt.combine(base_day, dtime(3, 0), tzinfo=tz),   # inside window
        _dt.combine(base_day, dtime(5, 0), tzinfo=tz),   # still inside window
        _dt.combine(base_day, dtime(6, 0), tzinfo=tz),   # exactly at deadline
        _dt.combine(base_day, dtime(6, 1), tzinfo=tz),   # just past deadline
        _dt.combine(base_day, dtime(9, 0), tzinfo=tz),   # well past deadline
    ]
    # Pad out to a healthy number of extra past-deadline cycles (well past
    # what a single deadline check would need), reusing the last, clearly
    # past-deadline `now` for the remainder.
    while len(now_points) < 30:
        now_points.append(now_points[-1] + timedelta(hours=1))

    orchestrator._cycle_active_date = test_date

    for now in now_points:
        orchestrator._cycle_now = now
        rows = await helpers.get_post_trade_rows(session_factory, test_date)
        by_code = {r.segment_code: r for r in rows}
        dmrpt = by_code["DMRPT"]
        if repository.is_handled(dmrpt):
            break
        await orchestrator._process_one_post_trade("DMRPT")

    async with session_factory() as session:
        final_rows = await repository.get_all_for_date(session, test_date)
    final_by_code = {r.segment_code: r for r in final_rows}
    dmrpt_final = final_by_code["DMRPT"]

    TERMINAL = {SegmentStatus.COMPLETED, SegmentStatus.FAILED, SegmentStatus.SKIPPED}

    print(
        f"\n[BUG HUNT RESULT] DMRPT final segment_status={dmrpt_final.segment_status!r} "
        f"current_state={dmrpt_final.current_state!r} "
        f"skip_category={dmrpt_final.skip_category!r} "
        f"skip_reason={dmrpt_final.skip_reason!r}\n"
    )

    if dmrpt_final.segment_status not in TERMINAL:
        # CONFIRMS the suspected bug: DMRPT is stuck (PENDING/IN_PROGRESS)
        # forever, with zero escalation, even after simulating time far
        # past the window deadline.
        assert dmrpt_final.segment_status in (SegmentStatus.PENDING, SegmentStatus.IN_PROGRESS)
        print(
            "[BUG CONFIRMED] DMRPT never reached a terminal state despite 30 cycles "
            "spanning times well past the 06:00 IST deadline. The window-deadline "
            "FAILED/TIMEOUT check in orchestrator._process_one_post_trade() only "
            "applies while row.segment_status == SegmentStatus.PENDING, but DMRPT "
            "flips to IN_PROGRESS on its first cycle -- before "
            "_check_previous_process_terminal() ever runs -- so that safety net can "
            "never fire again. DMRPT is permanently BLOCKED with no escalation."
        )
    else:
        print(
            f"[BUG NOT PRESENT] DMRPT correctly reached terminal status "
            f"{dmrpt_final.segment_status!r} -- the deadline check does cover this case."
        )
