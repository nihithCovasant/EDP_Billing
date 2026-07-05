"""
Pipeline executor — drives a single segment_execution row through its state
machine. Shared by both pipelines that live in this table:
  - the 7-step pipeline for the 7 real segments (CASH/EQ, F&O/DR, CD/CUR,
    SLBM/SL, MCX, NCDEX, MTF) — MTF is not special-cased.
  - the 3-step pipeline for the 5 T+1 post-trade processes (COLVAL, COLALLOC,
    MTFFT, DMRPT, DMSTMT).
The two are distinguished purely by which phase_handlers dict is passed in —
the loop mechanics (window deadline check, terminal signals, ADVANCE chaining)
are identical either way.
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
)
from .post_trade_stages import (
    handle_await_gtg,
    handle_trigger_job,
    handle_await_confirm,
)
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


_PHASE_HANDLERS = {
    # 7-step pipeline — shared by all 7 segments (EQ, DR, CUR, SL, MCX, NCDEX, MTF)
    SegmentPhase.HOLIDAY_CHECK:         handle_holiday_check,
    SegmentPhase.RESERVE_PID:           handle_reserve_pid,
    SegmentPhase.AWAIT_FILE_UPLOAD:     handle_await_file_upload,
    SegmentPhase.TRIGGER:               handle_trigger,
    SegmentPhase.AWAIT_BILLPOSTING:     handle_await_billposting,
    SegmentPhase.AWAIT_RECON:           handle_await_recon,
    SegmentPhase.AWAIT_CONTRACT_NOTE:   handle_await_contract_note,
}

_POST_TRADE_PHASE_HANDLERS = {
    # 3-step pipeline — shared by all 5 T+1 post-trade processes
    # (COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT)
    SegmentPhase.AWAIT_GTG:      handle_await_gtg,
    SegmentPhase.TRIGGER_JOB:    handle_trigger_job,
    SegmentPhase.AWAIT_CONFIRM:  handle_await_confirm,
}

_TERMINAL_SIGNALS = {StageResult.COMPLETED, StageResult.SKIPPED, StageResult.FAILED}


async def advance_pipeline(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
    window_end: datetime | None = None,
    phase_handlers: dict | None = None,
) -> str:
    """
    Execute pipeline stages for a segment_execution row until it blocks,
    completes, or fails.

    phase_handlers selects which pipeline drives this row — defaults to the
    7-step real-segment pipeline; pass _POST_TRADE_PHASE_HANDLERS (via
    pipeline.POST_TRADE_PHASE_HANDLERS) for the 5 post-trade processes.

    window_end is resolved by the caller (orchestrator._resolve_window) from
    workflow_json — it's no longer a stored column, just passed through.
    None for post-trade rows (no deadline, see orchestrator._resolve_post_trade_window).

    Returns one of: "completed" | "skipped" | "failed" | "advanced" | "blocked"
    """
    handlers = phase_handlers if phase_handlers is not None else _PHASE_HANDLERS
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
        handler = handlers.get(phase)

        if handler is None:
            if phase == SegmentPhase.DONE:
                return "completed"
            # An unmapped phase (e.g. a future migration adds a SegmentPhase
            # value without updating _PHASE_HANDLERS/_POST_TRADE_PHASE_HANDLERS)
            # must not just log-and-return — that leaves the row IN_PROGRESS
            # at the same unmapped phase forever, silently retried every
            # cycle with no visible failure anywhere but the logs. Mark it
            # FAILED and durably record why, same as any other stage error.
            logger.error(stage_log(
                row.segment_code, str(phase),
                "No handler registered for this phase — marking FAILED",
            ))
            row.segment_status = SegmentStatus.FAILED
            row.skip_category = "SYSTEM_ERROR"
            row.skip_reason = f"No pipeline handler registered for phase={phase}"
            row.completed_at = now
            await session.flush()
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
