"""
3-step pipeline stage handlers for the 5 T+1 post-trade processes (COLVAL,
COLALLOC, MTFFT, DMRPT, DMSTMT) — run once per trade_date, sequentially,
after (but independent of) the 7 real segments.

  AWAIT_GTG     -> POST file_process_status(<process-specific ProcessName>) — poll
  TRIGGER_JOB   -> POST <process-specific trigger endpoint>
  AWAIT_CONFIRM -> POST file_process_status(<same ProcessName>) — poll again

The 5 processes only differ in which CBOS trigger endpoint handle_trigger_job()
calls (dispatched on row.segment_code, fixed code — there's no CBOS
integration for an arbitrary process) — the GTG/confirm poll and overall
state machine are identical for all 5, including MTFFT (MTF Fund Transfer),
which the spec describes as "returns job execution result -> done" but which
is otherwise driven through the same re-poll-until-confirmed pattern as the
other 4 here for consistency.

The CBOS ProcessName used for the GTG/confirm polls IS ops-configurable
though (workflow_json["post_trade_processes"][].gtg_process_name, falling
back to a fixed default per process_code) — resolved once by the
orchestrator when the process starts and read here from row.current_process,
not re-derived from a hardcoded map on every poll (see
orchestrator._resolve_post_trade_process_name()).

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
    record_post_trade_trigger_attempt,
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
    # row.current_process was resolved from the active workflow config's
    # post_trade_processes[].gtg_process_name (or the fixed default) when
    # this process started — see orchestrator._resolve_post_trade_process_name()
    # — and is durably persisted, so it survives a restart mid-poll without
    # needing workflow_json re-fetched here.
    process_name = row.current_process or POST_TRADE_GTG_PROCESS_NAME.get(row.segment_code, row.segment_code)
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
    """
    POST the process-specific trigger endpoint (dispatched on segment_code).

    Crash safety: unlike the real-segment TRIGGER step, there is no
    PROCESSID/Table2 equivalent for CBOS's post-trade endpoints, so a
    resumed pod has no way to ask CBOS "did you actually get my last call?"
    before deciding whether it's safe to fire again. Given that, the only
    behaviour that can never double-trigger a real trading/collateral
    operation is: if we see our own "TRIGGERING" marker already sitting
    here (written durably, by commit(), right before the call last time),
    we know an attempt was made and its outcome is unknown — so we refuse
    to call the trigger endpoint again and mark the process FAILED with an
    explicit "needs manual CBOS verification" reason instead. An operator
    can then check CBOS directly and use the retry endpoint once they know
    it's actually safe.
    """
    code = row.segment_code
    method_name = _TRIGGER_DISPATCH.get(code)
    if not method_name:
        logger.error(stage_log(code, "TRIGGER_JOB", "Unknown post-trade process code — marking FAILED"))
        await _fail(row, "CBOS_ERROR", f"Unknown post-trade process code {code}", now)
        await session.flush()
        return StageResult.FAILED

    if get_proc(row, "trigger").get("status") == "TRIGGERING":
        logger.error(stage_log(
            code, "TRIGGER_JOB",
            "Resuming with an unconfirmed prior trigger attempt — post-trade jobs have no "
            "CBOS-side status check to safely verify, refusing to re-fire; marking FAILED "
            "for manual verification",
        ))
        await _fail(
            row, "CBOS_ERROR",
            "Unconfirmed trigger attempt after restart — verify with CBOS directly before "
            "retrying (post-trade triggers cannot be safely auto-recovered)",
            now,
        )
        await session.flush()
        return StageResult.FAILED

    # Pre-commit marker BEFORE the CBOS call, durably committed (not just
    # flushed) so a crash between this write and the outcome being recorded
    # can never silently revert to "never attempted" — see module docstring
    # above and record_post_trade_trigger_attempt()'s docstring.
    record_post_trade_trigger_attempt(row, now)
    await session.commit()

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
    # current_process already holds the resolved ProcessName from AWAIT_GTG
    # (see handle_await_gtg above) — left untouched rather than re-derived
    # from the fixed map, so a config-supplied gtg_process_name override
    # survives into AWAIT_CONFIRM too.
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
    process_name = row.current_process or POST_TRADE_GTG_PROCESS_NAME.get(row.segment_code, row.segment_code)
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
