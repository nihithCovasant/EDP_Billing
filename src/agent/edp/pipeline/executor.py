"""
Pipeline executor — drives a single segment through its 7-stage state machine.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SegmentExecution, SegmentPhase, SegmentStatus
from ..utils.datetime_utils import ensure_aware
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
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


_PHASE_HANDLERS = {
    SegmentPhase.HOLIDAY_CHECK:         handle_holiday_check,
    SegmentPhase.RESERVE_PID:           handle_reserve_pid,
    SegmentPhase.AWAIT_FILE_UPLOAD:     handle_await_file_upload,
    SegmentPhase.TRIGGER:               handle_trigger,
    SegmentPhase.AWAIT_BILLPOSTING:     handle_await_billposting,
    SegmentPhase.AWAIT_RECON:           handle_await_recon,
    SegmentPhase.AWAIT_CONTRACT_NOTE:   handle_await_contract_note,
}

_TERMINAL_SIGNALS = {StageResult.COMPLETED, StageResult.SKIPPED, StageResult.FAILED}


async def advance_pipeline(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> str:
    """
    Execute pipeline stages for a segment until it blocks, completes, or fails.

    Returns one of: "completed" | "skipped" | "failed" | "advanced" | "blocked"
    """
    while True:
        # Window deadline check — catches segments that stall in long polling phases
        window_end = ensure_aware(row.window_end_at)
        if (
            window_end
            and now > window_end
            and row.current_phase not in (SegmentPhase.DONE, None)
        ):
            logger.warning(stage_log(
                row.segment_code,
                row.current_phase.value,
                "Window deadline exceeded while IN_PROGRESS — marking TIMED_OUT",
                deadline=window_end.strftime("%H:%M:%S %Z"),
                now=now.strftime("%H:%M:%S %Z"),
                phase=row.current_phase.value,
            ))
            row.segment_status = SegmentStatus.FAILED
            row.skip_category = "TIMEOUT"
            row.skip_reason = (
                f"Exceeded window deadline {window_end.isoformat()} "
                f"at phase {row.current_phase.value}"
            )
            row.completed_at = now
            await session.flush()
            return "failed"

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
