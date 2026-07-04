"""
3-step pipeline stage handlers for the 5 T+1 post-trade processes (COLVAL,
COLALLOC, MTFFT, DMRPT, DMSTMT) — run once per trade_date, sequentially,
after (but independent of) the 7 real segments.

  AWAIT_GTG     -> POST file_process_status(<process-specific ProcessName>) — poll
  TRIGGER_JOB   -> POST <process-specific trigger endpoint>
  AWAIT_CONFIRM -> POST file_process_status(<same ProcessName>) — poll again

The 5 processes only differ in which CBOS trigger endpoint handle_trigger_job()
calls (dispatched on row.segment_code) — the GTG/confirm poll and overall
state machine are identical for all 5, including MTFFT (MTF Fund Transfer),
which the spec describes as "returns job execution result -> done" but which
is otherwise driven through the same re-poll-until-confirmed pattern as the
other 4 here for consistency.

Mirrors pipeline/stages.py's structure (StageResult, processes_json helpers,
_fail/_skip terminal helpers) — reused directly from there.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SegmentExecution, SegmentPhase, SegmentStatus
from ..utils.constants import POST_TRADE_GTG_PROCESS_NAME
from ..utils.json_helpers import (
    get_proc,
    inc_poll,
    mark_stage_done,
    record_post_trade_trigger,
    record_post_trade_trigger_failed,
)
from ..utils.log_fmt import stage_log
from .stages import StageResult, _fail, _skip
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


# ---------------------------------------------------------------------------
# Stage 1 — Await GTG (Good To Go)
# ---------------------------------------------------------------------------

async def handle_await_gtg(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """POST file_process_status(<ProcessName>) — poll until CBOS says ready to trigger."""
    process_name = POST_TRADE_GTG_PROCESS_NAME[row.segment_code]
    poll_state = get_proc(row, "gtg")
    poll_count = poll_state.get("poll_count", 0) + 1

    result = await cbos.file_process_status(
        segment=row.segment_code,
        process_name=process_name,
        user_id=login_id,
    )
    inc_poll(row, "gtg", result.response)
    await session.flush()

    if result.is_error:
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "AWAIT_GTG",
                "Transient CBOS error — will retry next cycle",
                error=result.error, poll=poll_count,
            ))
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "AWAIT_GTG",
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        await _fail(row, "CBOS_ERROR", f"{process_name} GTG check error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_skip:
        logger.info(stage_log(
            row.segment_code, "AWAIT_GTG",
            f"CBOS returned SKIP for {process_name} — process will be SKIPPED",
            response=result.response, poll=poll_count,
        ))
        await _skip(row, "CBOS_SKIP", f"{process_name} returned SKIP", now)
        await session.flush()
        return StageResult.SKIPPED

    if result.is_pending:
        if poll_count == 1 or poll_count % 5 == 0:
            logger.info(stage_log(
                row.segment_code, "AWAIT_GTG",
                f"{process_name} not yet ready — waiting",
                response=result.response, poll=poll_count,
            ))
        return StageResult.BLOCKED

    logger.info(stage_log(
        row.segment_code, "AWAIT_GTG",
        f"{process_name} GTG confirmed — proceeding to TRIGGER_JOB",
        response=result.response,
        total_polls=poll_count,
        ready_at=now.strftime("%H:%M:%S %Z"),
    ))
    mark_stage_done(row, "gtg", result.response, now)
    row.current_phase = SegmentPhase.TRIGGER_JOB
    row.current_process = None
    await session.flush()
    return StageResult.ADVANCE


# ---------------------------------------------------------------------------
# Stage 2 — Trigger the post-trade job
# ---------------------------------------------------------------------------

_TRIGGER_DISPATCH = {
    "COLVAL": "trigger_collateral_valuation",
    "COLALLOC": "trigger_collateral_allocation",
    "MTFFT": "trigger_mtf_fund_transfer",
    "DMRPT": "trigger_daily_margin_reporting",
    "DMSTMT": "trigger_daily_margin_statements",
}


async def handle_trigger_job(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """POST the process-specific trigger endpoint (dispatched on segment_code)."""
    code = row.segment_code
    method_name = _TRIGGER_DISPATCH.get(code)
    if not method_name:
        logger.error(stage_log(code, "TRIGGER_JOB", "Unknown post-trade process code — marking FAILED"))
        await _fail(row, "CBOS_ERROR", f"Unknown post-trade process code {code}", now)
        await session.flush()
        return StageResult.FAILED

    trigger_fn = getattr(cbos, method_name)
    logger.info(stage_log(
        code, "TRIGGER_JOB",
        "Firing post-trade trigger",
        triggered_at=now.strftime("%H:%M:%S %Z"),
    ))

    # All 5 trigger methods share the same (login_id, date) signature — only
    # the JSON key differs (MARGINDATE for COLVAL, TRADEDATE for the rest),
    # which is handled inside CbosClient itself.
    result = await trigger_fn(login_id, row.trade_date)

    if not result.success:
        record_post_trade_trigger_failed(row, result.error or "TRIGGER_FAILED", now)
        if result.is_transient:
            logger.warning(stage_log(
                code, "TRIGGER_JOB",
                "Transient CBOS error — will retry trigger next cycle",
                error=result.error,
            ))
            await session.flush()
            return StageResult.BLOCKED
        logger.error(stage_log(
            code, "TRIGGER_JOB",
            "Trigger FAILED — marking process FAILED",
            error=result.error,
        ))
        await _fail(row, "CBOS_ERROR", f"{method_name} failed: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    record_post_trade_trigger(row, result.message, now)
    row.current_phase = SegmentPhase.AWAIT_CONFIRM
    row.current_process = POST_TRADE_GTG_PROCESS_NAME[code]
    await session.flush()

    logger.info(stage_log(
        code, "TRIGGER_JOB",
        "Trigger acknowledged — will poll for confirmation next cycle",
        cbos_message=result.message,
        triggered_at=now.strftime("%H:%M:%S %Z"),
    ))
    return StageResult.STOP_NEXT


# ---------------------------------------------------------------------------
# Stage 3 — Await confirmation
# ---------------------------------------------------------------------------

async def handle_await_confirm(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """POST file_process_status(<ProcessName>) again — poll until CBOS confirms completion."""
    process_name = POST_TRADE_GTG_PROCESS_NAME[row.segment_code]
    poll_state = get_proc(row, "confirm")
    poll_count = poll_state.get("poll_count", 0) + 1

    result = await cbos.file_process_status(
        segment=row.segment_code,
        process_name=process_name,
        user_id=login_id,
    )
    inc_poll(row, "confirm", result.response)
    await session.flush()

    if result.is_error:
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "AWAIT_CONFIRM",
                "Transient CBOS error — will retry next cycle",
                error=result.error, poll=poll_count,
            ))
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "AWAIT_CONFIRM",
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        await _fail(row, "CBOS_ERROR", f"{process_name} confirm check error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_skip:
        logger.info(stage_log(
            row.segment_code, "AWAIT_CONFIRM",
            f"CBOS returned SKIP for {process_name} — process will be SKIPPED",
            response=result.response, poll=poll_count,
        ))
        await _skip(row, "CBOS_SKIP", f"{process_name} returned SKIP", now)
        await session.flush()
        return StageResult.SKIPPED

    if result.is_pending:
        if poll_count == 1 or poll_count % 5 == 0:
            logger.info(stage_log(
                row.segment_code, "AWAIT_CONFIRM",
                f"{process_name} not yet complete — waiting",
                response=result.response, poll=poll_count,
            ))
        return StageResult.BLOCKED

    logger.info(stage_log(
        row.segment_code, "AWAIT_CONFIRM",
        f"{process_name} CONFIRMED — post-trade process COMPLETED",
        response=result.response,
        total_polls=poll_count,
        confirmed_at=now.strftime("%H:%M:%S %Z"),
    ))
    mark_stage_done(row, "confirm", result.response, now)
    row.segment_status = SegmentStatus.COMPLETED
    row.current_process = None
    row.current_phase = SegmentPhase.DONE
    row.completed_at = now
    await session.flush()
    return StageResult.COMPLETED
