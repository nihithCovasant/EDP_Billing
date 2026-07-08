"""
Test harness — drives the real EdpOrchestrator through an isolated,
far-future trade_date without depending on wall-clock "today".

run_wake_cycle() always resolves active_date from the real current time,
so tests instead call orchestrator._process_one_segment()/
_process_one_post_trade() directly against a caller-supplied trade_date.
Everything below that level (pipeline stages, CBOS calls, state
transitions) is exercised exactly as in production.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import delete

from src.agent.edp import repository
from src.agent.edp.config import EdpBootstrapConfig, build_default_workflow_json
from src.agent.edp.models import EdpProperties, SegmentExecution, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import SEGMENT_ORDER, POST_TRADE_ORDER

TERMINAL_STATES = {SegmentStatus.COMPLETED, SegmentStatus.SKIPPED, SegmentStatus.FAILED}

ALL_SEGMENT_CODES = list(SEGMENT_ORDER)
ALL_POST_TRADE_CODES = list(POST_TRADE_ORDER)


def build_all_day_open_workflow_json() -> dict:
    """A workflow_json where every segment's window is wide open (00:00 ->
    23:59 next day, per the fixed overnight rule in _resolve_window()) so
    tests are never gated by window checks."""
    segments = [
        {
            "segment_code": code,
            "login_id": "CV0001",
            "window_start": "00:00",
            "window_end": "23:59",
        }
        for code in SEGMENT_ORDER
    ]
    return build_default_workflow_json(segments)


def fixed_now_for(trade_date: date, tz) -> datetime:
    """A stable "now" anchored to trade_date (noon) instead of real
    wall-clock time, matching how window boundaries are computed."""
    return datetime.combine(trade_date, dtime(12, 0), tzinfo=tz)


def fixed_post_trade_now_for(trade_date: date, tz) -> datetime:
    """A stable "now" for the post-trade processes, anchored to trade_date+1
    03:00 — inside Process 1's (COLVAL) 02:30-06:00 IST window."""
    return datetime.combine(trade_date + timedelta(days=1), dtime(3, 0), tzinfo=tz)


async def seed_day(session_factory, trade_date: date, cfg: EdpBootstrapConfig) -> None:
    """Upload an all-day-open workflow config and seed all segment rows."""
    workflow_json = build_all_day_open_workflow_json()
    async with session_factory() as session:
        await repository.upload(session, trade_date, workflow_json, uploaded_by="test")
        await session.commit()

    async with session_factory() as session:
        workflow = await repository.get_active(session, trade_date)
        await repository.seed_from_workflow(session, workflow, trade_date)
        await session.commit()


async def seed_post_trade_day(
    session_factory, trade_date: date,
) -> None:
    """
    Seed the 5 post-trade process rows for trade_date. If no workflow has
    been uploaded yet (standalone post-trade tests), uploads a minimal
    default one first so there's something to seed from.
    """
    async with session_factory() as session:
        workflow = await repository.get_active(session, trade_date)
        if not workflow:
            workflow_json = build_default_workflow_json([])
            workflow, _ = await repository.upload(
                session, trade_date, workflow_json, uploaded_by="test"
            )
            await session.commit()

    async with session_factory() as session:
        workflow = await repository.get_active(session, trade_date)
        await repository.seed_post_trade_processes(session, workflow, trade_date)
        await session.commit()


async def get_rows(session_factory, trade_date: date) -> list[SegmentExecution]:
    async with session_factory() as session:
        return await repository.get_all_for_date(session, trade_date)


async def get_post_trade_rows(session_factory, trade_date: date) -> list[SegmentExecution]:
    rows = await get_rows(session_factory, trade_date)
    by_code = {r.segment_code: r for r in rows if r.segment_code in POST_TRADE_ORDER}
    return [by_code[code] for code in POST_TRADE_ORDER if code in by_code]


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
    """One pass over the day's segments — every not-yet-handled row is
    attempted exactly once, independent of other segments' status."""
    rows = await get_rows(session_factory, trade_date)

    orchestrator._cycle_active_date = trade_date
    orchestrator._cycle_now = fixed_now_for(trade_date, orchestrator._tz)

    processed = 0
    for row in rows:
        if repository.is_handled(row):
            continue
        processed += 1
        await orchestrator._process_one_segment(row.segment_code)

    return {"processed": processed}


async def drive_until_terminal(
    orchestrator: EdpOrchestrator,
    session_factory,
    trade_date: date,
    max_cycles: int = 150,
) -> list[SegmentExecution]:
    """
    Repeatedly runs wake-cycle-equivalent passes until every segment for
    the day reaches a terminal state (segments are independent, so one
    FAILED doesn't stop the others). Raises TimeoutError past max_cycles.
    """
    for _ in range(max_cycles):
        await run_one_cycle(orchestrator, session_factory, trade_date)
        rows = await get_rows(session_factory, trade_date)

        if all(r.segment_status in TERMINAL_STATES for r in rows):
            return rows

    raise TimeoutError(
        f"Day {trade_date} did not reach a terminal state within {max_cycles} cycles"
    )


async def run_one_post_trade_cycle(
    orchestrator: EdpOrchestrator, session_factory, trade_date: date,
) -> dict:
    """One pass over the day's 5 post-trade processes, anchored to a fixed
    "now" inside Process 1's window. Post-trade equivalent of run_one_cycle()."""
    rows = await get_post_trade_rows(session_factory, trade_date)

    orchestrator._cycle_active_date = trade_date
    orchestrator._cycle_now = fixed_post_trade_now_for(trade_date, orchestrator._tz)

    processed = 0
    for row in rows:
        if repository.is_handled(row):
            continue
        processed += 1
        await orchestrator._process_one_post_trade(row.segment_code)

    return {"processed": processed}


async def drive_post_trade_until_terminal(
    orchestrator: EdpOrchestrator,
    session_factory,
    trade_date: date,
    max_cycles: int = 150,
) -> list[SegmentExecution]:
    """Post-trade equivalent of drive_until_terminal() — see that docstring."""
    for _ in range(max_cycles):
        await run_one_post_trade_cycle(orchestrator, session_factory, trade_date)
        rows = await get_post_trade_rows(session_factory, trade_date)

        if all(r.segment_status in TERMINAL_STATES for r in rows):
            return rows

    raise TimeoutError(
        f"Post-trade chain for {trade_date} did not reach a terminal state within {max_cycles} cycles"
    )
