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
from ..repository.segment import move_to_state
from ..utils.constants import is_post_trade_process
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


def get_segment_state_handler(segment_code: str, phase: SegmentPhase | None):
    """Resolve the handler for (segment_code, phase). Returns None for DONE
    or an unmapped/corrupt phase."""
    handlers = _POST_TRADE_PHASE_HANDLERS if is_post_trade_process(segment_code) else _PHASE_HANDLERS
    return handlers.get(phase)


async def advance_pipeline(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
    window_end: datetime | None = None,
) -> str:
    """
    Execute pipeline stages for a row until it blocks, completes, or fails.
    window_end is None for post-trade rows (no deadline).
    Returns one of: "completed" | "skipped" | "failed" | "advanced" | "blocked"
    """
    window_end = ensure_aware(window_end)
    while True:
        # Refresh every iteration so a chain of instant ADVANCEs doesn't run
        # against a stale timestamp from the start of the wake cycle.
        now = now_ist()

        # Catches segments that stall in long polling phases past the window.
        # This is a local timeout, not a CBOS-driven skip signal, so it's a
        # FAILED outcome (category TIMEOUT), not SKIPPED.
        if (
            window_end
            and now > window_end
            and row.current_phase not in (SegmentPhase.DONE, None)
        ):
            timed_out_phase = row.current_phase.value
            logger.warning(stage_log(
                row.segment_code,
                timed_out_phase,
                "Window deadline exceeded while IN_PROGRESS — marking FAILED",
                deadline=window_end.strftime("%H:%M:%S %Z"),
                now=now.strftime("%H:%M:%S %Z"),
                phase=timed_out_phase,
            ))
            await move_to_state(
                session, row, SegmentStatus.FAILED,
                category="TIMEOUT",
                reason=f"Exceeded window deadline {window_end.isoformat()} at phase {timed_out_phase}",
                now=now,
            )
            return "failed"

        phase = row.current_phase
        handler = get_segment_state_handler(row.segment_code, phase)

        if handler is None:
            if phase == SegmentPhase.DONE:
                return "completed"
            # An unmapped phase must not silently retry forever — fail it durably.
            logger.error(stage_log(
                row.segment_code, str(phase),
                "No handler registered for this phase — marking FAILED",
            ))
            await move_to_state(
                session, row, SegmentStatus.FAILED,
                category="SYSTEM_ERROR",
                reason=f"No pipeline handler registered for phase={phase}",
                now=now,
            )
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
