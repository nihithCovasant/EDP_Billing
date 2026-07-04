"""
Pipeline executor — drives a single segment through its 7-stage state machine.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SegmentExecution, SegmentPhase, SegmentStatus
from ..utils.datetime_utils import ensure_aware, now_ist
from ..utils.log_fmt import stage_log
from .stages import (
    StageResult,
    handle_holiday_check,
    handle_reserve_pid,
    handle_await_file_upload,
    handle_trigger,
    handle_await_billposting,
    handle_await_recon,
    handle_await_contract_note,
    handle_collateral_valuation,
    handle_collateral_allocation,
    handle_fund_transfer,
    handle_mtf_buy,
    handle_mtf_sell,
    handle_weekly_auto_closure,
)
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


_PHASE_HANDLERS = {
    # 7-stage per-segment pipeline (EQ, DR, CUR, SLB, NCDEX, MCX, NSECOM, MF)
    SegmentPhase.HOLIDAY_CHECK:         handle_holiday_check,
    SegmentPhase.RESERVE_PID:           handle_reserve_pid,
    SegmentPhase.AWAIT_FILE_UPLOAD:     handle_await_file_upload,
    SegmentPhase.TRIGGER:               handle_trigger,
    SegmentPhase.AWAIT_BILLPOSTING:     handle_await_billposting,
    SegmentPhase.AWAIT_RECON:           handle_await_recon,
    SegmentPhase.AWAIT_CONTRACT_NOTE:   handle_await_contract_note,
    # 6-stage post-segment MTF operations chain (virtual MTFOPS segment)
    SegmentPhase.COLLATERAL_VALUATION:  handle_collateral_valuation,
    SegmentPhase.COLLATERAL_ALLOCATION: handle_collateral_allocation,
    SegmentPhase.FUND_TRANSFER:         handle_fund_transfer,
    SegmentPhase.MTF_BUY:               handle_mtf_buy,
    SegmentPhase.MTF_SELL:              handle_mtf_sell,
    SegmentPhase.WEEKLY_AUTO_CLOSURE:   handle_weekly_auto_closure,
}

_TERMINAL_SIGNALS = {StageResult.COMPLETED, StageResult.SKIPPED, StageResult.FAILED}


async def advance_pipeline(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
    window_end: datetime | None = None,
) -> str:
    """
    Execute pipeline stages for a segment until it blocks, completes, or fails.

    window_end is resolved by the caller (orchestrator._resolve_window) from
    workflow_json — it's no longer a stored column, just passed through.

    Returns one of: "completed" | "skipped" | "failed" | "advanced" | "blocked"
    """
    window_end = ensure_aware(window_end)
    while True:
        # Refresh "now" every iteration — a chain of instant ADVANCE results
        # (multiple phases completing back-to-back within one wake cycle) can
        # otherwise run for a noticeable time against the stale timestamp
        # captured once at the start of the wake cycle.
        now = now_ist()

        # Window deadline check — catches segments that stall in long polling phases
        if (
            window_end
            and now > window_end
            and row.current_phase not in (SegmentPhase.DONE, None)
        ):
            timed_out_phase = row.current_phase.value
            logger.warning(stage_log(
                row.segment_code,
                timed_out_phase,
                "Window deadline exceeded while IN_PROGRESS — SKIPPING segment, "
                "moving on to the next segment in sequence",
                deadline=window_end.strftime("%H:%M:%S %Z"),
                now=now.strftime("%H:%M:%S %Z"),
                phase=timed_out_phase,
            ))
            row.segment_status = SegmentStatus.SKIPPED
            row.skip_category = "TIMEOUT"
            row.skip_reason = (
                f"Exceeded window deadline {window_end.isoformat()} "
                f"at phase {timed_out_phase}"
            )
            row.current_phase = SegmentPhase.DONE
            row.completed_at = now
            await session.flush()
            return "skipped"

        phase = row.current_phase
        handler = _PHASE_HANDLERS.get(phase)

        if handler is None:
            if phase == SegmentPhase.DONE:
                return "completed"
            logger.error(stage_log(
                row.segment_code, str(phase),
                "No handler registered for this phase — unexpected state",
            ))
            return "failed"

        result: StageResult = await handler(cbos, row, session, login_id, now)

        if result in _TERMINAL_SIGNALS:
            return result.value

        if result == StageResult.BLOCKED:
            return "blocked"

        if result == StageResult.STOP_NEXT:
            return "advanced"

        # ADVANCE — log the transition and run the next phase immediately
        next_phase = row.current_phase
        logger.info(stage_log(
            row.segment_code,
            phase.value,
            f"Phase complete — advancing to {next_phase.value}",
        ))
