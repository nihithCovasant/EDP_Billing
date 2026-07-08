"""
Pipeline executor — drives a single segment_execution row through its state
machine. Shared by both pipelines that live in this table:
  - the 7-step pipeline for the 7 real segments (CASH/EQ, F&O/DR, CD/CUR,
    SLBM/SL, MCX, NCDEX, MTF) — MTF is not special-cased.
  - the 3-step pipeline for the 5 T+1 post-trade processes (COLVAL, COLALLOC,
    MTFFT, DMRPT, DMSTMT).
Which family a row belongs to is derived from its segment_code (via
is_post_trade_process) rather than passed in by the caller — one source of
truth, so a row can never be driven against the wrong family by a
caller-side mistake. Dispatch is an explicit switch-on-family-then-
switch-on-phase (match/case) — the loop mechanics (window deadline check,
terminal signals, ADVANCE chaining) are identical either way.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from .. import alerts
from ..models import SegmentExecution, SegmentPhase, SegmentStatus
from ..utils.constants import is_post_trade_process
from ..utils.datetime_utils import ensure_aware, now_ist
from ..utils.log_fmt import stage_log
from ..utils.serializers import serialize_segment_alert
from .stages import (
    StageResult,
    _fail,
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


_TERMINAL_SIGNALS = {StageResult.COMPLETED, StageResult.SKIPPED, StageResult.FAILED}


def get_segment_state_handler(segment_code: str, phase: SegmentPhase | None):
    """
    Switch on segment family first (real 7-step segments vs the 5 T+1
    post-trade processes — each has its own disjoint set of valid phases),
    then switch on phase within that family — mirrors the manager's
    get_segment_state_handler(segment, segment_state) sketch.

    Unlike the sketch, this never returns a sentinel for "no handler" —
    it raises ValueError instead, so a lookup miss can't be accidentally
    ignored by a future caller. advance_pipeline() below is the single
    place that catches that raise and turns it into a domain-level FAILED
    row (SYSTEM_ERROR); this function's only job is the lookup, not
    deciding what a lookup miss should mean for the row's business state.
    """
    if is_post_trade_process(segment_code):
        match phase:
            case SegmentPhase.AWAIT_GTG:
                return handle_await_gtg
            case SegmentPhase.TRIGGER_JOB:
                return handle_trigger_job
            case SegmentPhase.AWAIT_CONFIRM:
                return handle_await_confirm
            case _:
                logger.error(stage_log(
                    segment_code, str(phase),
                    "No post-trade handler registered for this phase",
                ))
                raise ValueError(
                    f"No post-trade pipeline handler for segment={segment_code} phase={phase}"
                )
    else:
        match phase:
            case SegmentPhase.HOLIDAY_CHECK:
                return handle_holiday_check
            case SegmentPhase.RESERVE_PID:
                return handle_reserve_pid
            case SegmentPhase.AWAIT_FILE_UPLOAD:
                return handle_await_file_upload
            case SegmentPhase.TRIGGER:
                return handle_trigger
            case SegmentPhase.AWAIT_BILLPOSTING:
                return handle_await_billposting
            case SegmentPhase.AWAIT_RECON:
                return handle_await_recon
            case SegmentPhase.AWAIT_CONTRACT_NOTE:
                return handle_await_contract_note
            case _:
                logger.error(stage_log(
                    segment_code, str(phase),
                    "No handler registered for this phase",
                ))
                raise ValueError(
                    f"No pipeline handler for segment={segment_code} phase={phase}"
                )


async def advance_pipeline(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
    window_end: datetime | None = None,
) -> str:
    """
    Execute pipeline stages for a segment_execution row until it blocks,
    completes, or fails. Which of the two families drives this row's phase
    dispatch is derived from row.segment_code (see get_segment_state_handler),
    not passed in — the row can't be driven against the wrong family by a
    caller-side mistake. window_end is None for post-trade rows (no deadline).

    Returns one of: "completed" | "skipped" | "failed" | "advanced" | "blocked"
    """
    window_end = ensure_aware(window_end)
    while True:
        # Refresh every iteration so a chain of instant ADVANCEs doesn't run
        # against a stale timestamp from the start of the wake cycle.
        now = now_ist()

        # Catches segments that stall in long polling phases past the window.
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
            await alerts.send_timeout_alert(serialize_segment_alert(row))
            return "skipped"

        phase = row.current_phase
        if phase == SegmentPhase.DONE:
            return "completed"

        try:
            handler = get_segment_state_handler(row.segment_code, phase)
        except ValueError as exc:
            # An unmapped phase must not silently retry forever — fail it durably.
            await _fail(row, "SYSTEM_ERROR", str(exc), now)
            await session.flush()
            return "failed"

        result: StageResult = await handler(cbos, row, session, login_id, now)

        if result in _TERMINAL_SIGNALS:
            return result.value

        if result == StageResult.BLOCKED:
            return "blocked"

        if result == StageResult.STOP_NEXT:
            return "advanced"

        # ADVANCE — the handler already logged the specific transition
        # (with its own context: response, poll count, etc). Loop straight
        # into the next phase rather than logging a second, generic line
        # for the same transition.
