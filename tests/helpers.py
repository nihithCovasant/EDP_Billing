"""
Test harness — drives the real EdpOrchestrator through an isolated,
far-future trade_date without depending on wall-clock "today".

Why not just call orchestrator.run_wake_cycle()?
--------------------------------------------------
run_wake_cycle() always resolves active_date from the real current time
(resolve_active_date(datetime.now(...), cutoff_hour, tz)) — it has no way
to target an arbitrary test date. Calling it directly from a test would
process *today's* real segment rows, which is exactly the data a live
agent instance (see loop.py) is concurrently working on. That would make
tests flaky and, worse, could corrupt real operational state.

Instead, these helpers replicate the same "seed config -> iterate segments
in sequence, halting on FAILED" logic run_wake_cycle() uses, but pointed at
a caller-supplied trade_date, by driving orchestrator._process_one_segment()
directly (the same method run_wake_cycle() calls internally). Everything
below the per-segment level (locking, pipeline stages, CBOS calls,
sequencing/halt-on-FAILED rules) is exercised exactly as in production.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime

from sqlalchemy import delete

from src.agent.edp import repository
from src.agent.edp.config import EdpBootstrapConfig, build_default_workflow_json
from src.agent.edp.models import EdpProperties, SegmentExecution, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import MTF_OPS_SEGMENT_CODE, SEGMENT_ORDER

TERMINAL_STATES = {SegmentStatus.COMPLETED, SegmentStatus.SKIPPED, SegmentStatus.FAILED}

ALL_SEGMENT_CODES = list(SEGMENT_ORDER) + [MTF_OPS_SEGMENT_CODE]


def build_all_day_open_workflow_json(timezone: str = "Asia/Kolkata") -> dict:
    """
    A workflow_json where every real segment's window is wide open
    (00:00 -> 23:59 the next day) so tests are never gated by window
    checks no matter when they actually run — only the pipeline mechanics
    (locking, stage sequencing, CBOS responses) are under test here, not
    wall-clock scheduling.
    """
    segments = [
        {
            "segment_code": code,
            "login_id": "CV0001",
            "window_start": "00:00",
            "window_end": "23:59",
            "window_end_next_day": True,
        }
        for code in SEGMENT_ORDER
    ]
    return build_default_workflow_json(segments, timezone=timezone)


def fixed_now_for(trade_date: date, tz) -> datetime:
    """
    A stable "now" anchored to trade_date (noon) instead of real wall-clock
    time. Window boundaries (see build_all_day_open_workflow_json) are
    computed from trade_date too, so this must be anchored the same way —
    using real wall-clock "now" here would compare "today" against a
    window built around a far-future trade_date and always read as
    "window not open yet".
    """
    return datetime.combine(trade_date, dtime(12, 0), tzinfo=tz)


async def seed_day(session_factory, trade_date: date, cfg: EdpBootstrapConfig) -> None:
    """Upload an all-day-open workflow config and seed all segment rows —
    mirrors the setup steps orchestrator.run_wake_cycle() performs before
    driving segments, minus the wall-clock active_date resolution."""
    workflow_json = build_all_day_open_workflow_json(cfg.timezone)
    async with session_factory() as session:
        await repository.upload(session, trade_date, workflow_json, uploaded_by="test")
        await session.commit()

    async with session_factory() as session:
        workflow = await repository.get_active(session, trade_date)
        await repository.seed_from_workflow(session, workflow, trade_date)
        await repository.seed_mtf_ops_segment(session, workflow, trade_date)
        await session.commit()


async def get_rows(session_factory, trade_date: date) -> list[SegmentExecution]:
    async with session_factory() as session:
        return await repository.get_all_for_date(session, trade_date)


async def cleanup_day(session_factory, trade_date: date) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(SegmentExecution).where(SegmentExecution.trade_date == trade_date)
        )
        await session.execute(
            delete(EdpProperties).where(EdpProperties.trade_date == trade_date)
        )
        await session.commit()


async def run_one_cycle(orchestrator: EdpOrchestrator, session_factory, trade_date: date) -> dict:
    """
    One pass over the day's segments in sequence order — mirrors the
    "Drive each segment in sequence" loop body in
    orchestrator.run_wake_cycle(): skip terminal COMPLETED/SKIPPED rows,
    halt immediately on a FAILED row, otherwise process exactly one
    non-terminal segment and stop the pass unless it fully finished
    ("completed"/"skipped") in this single call.
    """
    rows = await get_rows(session_factory, trade_date)

    # Same per-cycle snapshot orchestrator.run_wake_cycle() takes, just
    # anchored to trade_date instead of real wall-clock "now".
    orchestrator._cycle_active_date = trade_date
    orchestrator._cycle_now = fixed_now_for(trade_date, orchestrator._tz)

    processed = 0
    halted_on_failure = False
    for row in rows:
        status = row.segment_status
        if status in (SegmentStatus.COMPLETED, SegmentStatus.SKIPPED):
            continue
        if status == SegmentStatus.FAILED:
            halted_on_failure = True
            break

        processed += 1
        outcome = await orchestrator._process_one_segment(row.segment_code)
        if outcome not in ("completed", "skipped"):
            break

    return {"processed": processed, "halted_on_failure": halted_on_failure}


async def drive_until_terminal(
    orchestrator: EdpOrchestrator,
    session_factory,
    trade_date: date,
    max_cycles: int = 150,
) -> list[SegmentExecution]:
    """
    Repeatedly runs wake-cycle-equivalent passes until either:
      - every segment for the day has reached a terminal state, or
      - the chain has permanently halted on a FAILED segment (everything
        after it will stay PENDING forever — that IS the expected/correct
        outcome, not something to keep looping on).

    Raises TimeoutError if neither happens within max_cycles — that
    indicates a real bug (e.g. an infinite BLOCKED loop), not just a slow
    test, since the in-process CBOS mock always resolves within a handful
    of polls.
    """
    for _ in range(max_cycles):
        result = await run_one_cycle(orchestrator, session_factory, trade_date)
        rows = await get_rows(session_factory, trade_date)

        if result["halted_on_failure"]:
            return rows
        if all(r.segment_status in TERMINAL_STATES for r in rows):
            return rows

    raise TimeoutError(
        f"Day {trade_date} did not reach a terminal state within {max_cycles} cycles"
    )
