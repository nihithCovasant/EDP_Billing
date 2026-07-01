"""
7-stage pipeline stage handlers.

Each handler performs exactly one CBOS API call, updates processes_json,
mutates the row phase/process fields, and returns a StageResult.

StageResult values:
  ADVANCE    — stage done; move to next phase in the same cycle
  BLOCKED    — CBOS returned FALSE (not ready) or transient error; come back next cycle
  STOP_NEXT  — trigger fired; start polling on next cycle
  COMPLETED  — all 7 stages done; segment is COMPLETED
  SKIPPED    — holiday gate returned SKIP
  FAILED     — permanent error; segment is FAILED
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SegmentExecution, SegmentPhase, SegmentStatus
from ..utils.json_helpers import (
    inc_poll,
    mark_stage_done,
    record_trigger,
    record_trigger_failed,
    set_proc,
    get_proc,
)
from ..utils.log_fmt import stage_log
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


class StageResult(str, Enum):
    ADVANCE = "advance"
    BLOCKED = "blocked"
    STOP_NEXT = "stop_next"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Stage 1 — Holiday Check
# ---------------------------------------------------------------------------

async def handle_holiday_check(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """
    POST file_process_status(BeginFileUpload)
    SKIP → holiday  |  FALSE → not yet open  |  TRUE → proceed
    """
    logger.info(stage_log(row.segment_code, "HOLIDAY_CHECK", "Checking holiday gate (BeginFileUpload)"))

    result = await cbos.file_process_status(
        segment=row.segment_code,
        process_name="BeginFileUpload",
        user_id=login_id,
    )
    poll_state = get_proc(row, "holiday_check")
    poll_count = poll_state.get("poll_count", 0) + 1
    inc_poll(row, "holiday_check", result.response)
    await session.flush()

    if result.is_error:
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "HOLIDAY_CHECK",
                "Transient CBOS error — will retry next cycle",
                error=result.error,
                poll=poll_count,
            ))
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "HOLIDAY_CHECK",
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        _fail(row, "CBOS_ERROR", f"BeginFileUpload error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_skip:
        logger.info(stage_log(
            row.segment_code, "HOLIDAY_CHECK",
            "Market HOLIDAY — segment will be SKIPPED",
            response=result.response,
            at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_stage_done(row, "holiday_check", result.response, now)
        _skip(row, "CBOS_SKIP", "BeginFileUpload returned SKIP — market holiday", now)
        await session.flush()
        return StageResult.SKIPPED

    if result.is_pending:
        logger.info(stage_log(
            row.segment_code, "HOLIDAY_CHECK",
            "EDP window not yet open — will check next cycle",
            response=result.response,
            poll=poll_count,
        ))
        return StageResult.BLOCKED

    # TRUE — good to go
    logger.info(stage_log(
        row.segment_code, "HOLIDAY_CHECK",
        "Holiday check PASSED — proceeding to RESERVE_PID",
        response=result.response,
        at=now.strftime("%H:%M:%S %Z"),
    ))
    mark_stage_done(row, "holiday_check", result.response, now)
    row.current_phase = SegmentPhase.RESERVE_PID
    row.current_process = None
    await session.flush()
    return StageResult.ADVANCE


# ---------------------------------------------------------------------------
# Stage 2 — Reserve Process ID
# ---------------------------------------------------------------------------

async def handle_reserve_pid(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """
    POST getNewTradeProcess(PROCESSID="0") — allocates a new PROCESSID from CBOS.
    """
    logger.info(stage_log(
        row.segment_code, "RESERVE_PID",
        "Reserving process ID from CBOS (PROCESSID=0)",
        trade_date=str(row.trade_date),
    ))

    result = await cbos.get_new_trade_process(
        group_name=row.segment_code,
        login_id=login_id,
        trade_date=row.trade_date,
        process_id="0",
    )

    if not result.success or not result.process_id:
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "RESERVE_PID",
                "Transient CBOS error — will retry next cycle",
                error=result.error,
            ))
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "RESERVE_PID",
            "Failed to allocate process ID — marking FAILED",
            error=result.error,
        ))
        _fail(row, "CBOS_ERROR", f"getNewTradeProcess(PROCESSID=0) failed: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    row.process_id = result.process_id
    row.process_id_reserved_at = now
    set_proc(row, "trigger", {
        "status": "PID_RESERVED",
        "process_id_reserved": result.process_id,
        "reserved_at": now.isoformat(),
        "is_runnable": result.is_runnable,
        "is_auto_upload": result.is_auto_upload,
    })
    row.current_phase = SegmentPhase.AWAIT_FILE_UPLOAD
    row.current_process = "FILEUPLOAD"
    await session.flush()

    logger.info(stage_log(
        row.segment_code, "RESERVE_PID",
        "Process ID reserved — proceeding to AWAIT_FILE_UPLOAD",
        pid=result.process_id,
        is_runnable=result.is_runnable,
        is_auto_upload=result.is_auto_upload,
        reserved_at=now.strftime("%H:%M:%S %Z"),
    ))
    return StageResult.ADVANCE


# ---------------------------------------------------------------------------
# Stage 3 — Await File Upload
# ---------------------------------------------------------------------------

async def handle_await_file_upload(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """
    POST file_process_status(FILEUPLOAD) — poll until exchange files are uploaded.
    """
    poll_state = get_proc(row, "file_upload_ready")
    poll_count = poll_state.get("poll_count", 0) + 1

    result = await cbos.file_process_status(
        segment=row.segment_code,
        process_name="FILEUPLOAD",
        user_id=login_id,
    )
    inc_poll(row, "file_upload_ready", result.response)
    await session.flush()

    if result.is_error:
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "AWAIT_FILE_UPLOAD",
                "Transient CBOS error — will retry next cycle",
                error=result.error, poll=poll_count,
            ))
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "AWAIT_FILE_UPLOAD",
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        _fail(row, "CBOS_ERROR", f"FILEUPLOAD check error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_pending:
        # Log every 5 polls to avoid flooding logs, always log first
        if poll_count == 1 or poll_count % 5 == 0:
            logger.info(stage_log(
                row.segment_code, "AWAIT_FILE_UPLOAD",
                "Exchange files not yet uploaded — waiting",
                response=result.response,
                poll=poll_count,
            ))
        return StageResult.BLOCKED

    logger.info(stage_log(
        row.segment_code, "AWAIT_FILE_UPLOAD",
        "All exchange files uploaded — proceeding to TRIGGER",
        response=result.response,
        total_polls=poll_count,
        ready_at=now.strftime("%H:%M:%S %Z"),
    ))
    mark_stage_done(row, "file_upload_ready", result.response, now)
    row.current_phase = SegmentPhase.TRIGGER
    row.current_process = None
    await session.flush()
    return StageResult.ADVANCE


# ---------------------------------------------------------------------------
# Stage 4 — Trigger Processing
# ---------------------------------------------------------------------------

async def handle_trigger(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """
    POST getNewTradeProcess(PROCESSID=<actual>) — starts billing/calculation.
    After firing, polls BILLPOSTING from next wake cycle.
    """
    # Safety: recover process_id if missing (crash/restart scenario)
    if not row.process_id:
        logger.warning(stage_log(
            row.segment_code, "TRIGGER",
            "process_id missing — attempting crash recovery from CBOS",
            trade_date=str(row.trade_date),
        ))
        recovery = await cbos.get_existing_process_id(
            segment=row.segment_code,
            login_id=login_id,
            trade_date=row.trade_date,
        )
        if recovery.found and recovery.process_id:
            row.process_id = recovery.process_id
            logger.info(stage_log(
                row.segment_code, "TRIGGER",
                "process_id recovered from CBOS",
                pid=recovery.process_id,
                desc=recovery.description,
            ))
        else:
            logger.error(stage_log(
                row.segment_code, "TRIGGER",
                "Cannot recover process_id — marking FAILED",
                error=recovery.error,
            ))
            _fail(row, "CBOS_ERROR", "No process_id available for trigger", now)
            await session.flush()
            return StageResult.FAILED

    logger.info(stage_log(
        row.segment_code, "TRIGGER",
        "Firing process trigger (getNewTradeProcess)",
        pid=row.process_id,
        trade_date=str(row.trade_date),
        triggered_at=now.strftime("%H:%M:%S %Z"),
    ))

    result = await cbos.get_new_trade_process(
        group_name=row.segment_code,
        login_id=login_id,
        trade_date=row.trade_date,
        process_id=row.process_id,
    )

    if not result.success:
        record_trigger_failed(row, result.error or "TRIGGER_FAILED", now)
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "TRIGGER",
                "Transient CBOS error — will retry trigger next cycle",
                pid=row.process_id,
                error=result.error,
            ))
            await session.flush()
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "TRIGGER",
            "Trigger FAILED — marking segment FAILED",
            pid=row.process_id,
            error=result.error,
        ))
        _fail(
            row, "CBOS_ERROR",
            f"getNewTradeProcess(PROCESSID={row.process_id}) failed: {result.error}", now
        )
        await session.flush()
        return StageResult.FAILED

    record_trigger(row, row.process_id, result.is_runnable, now)
    row.current_phase = SegmentPhase.AWAIT_BILLPOSTING
    row.current_process = "BILLPOSTING"
    await session.flush()

    logger.info(stage_log(
        row.segment_code, "TRIGGER",
        "Process TRIGGERED successfully — will poll BILLPOSTING next cycle",
        pid=row.process_id,
        is_runnable=result.is_runnable,
        triggered_at=now.strftime("%H:%M:%S %Z"),
    ))
    return StageResult.STOP_NEXT


# ---------------------------------------------------------------------------
# Stages 5 / 6 / 7 — Poll CBOS completion status
# ---------------------------------------------------------------------------

async def handle_await_billposting(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """POST file_process_status(BILLPOSTING) — wait until billing calculations complete."""
    return await _poll_confirmation(
        cbos, row, session, login_id, now,
        process_name="BILLPOSTING",
        stage_key="bill_posting",
        next_phase=SegmentPhase.AWAIT_RECON,
        next_process="RECON",
    )


async def handle_await_recon(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """POST file_process_status(RECON) — wait until reconciliation completes."""
    return await _poll_confirmation(
        cbos, row, session, login_id, now,
        process_name="RECON",
        stage_key="recon",
        next_phase=SegmentPhase.AWAIT_CONTRACT_NOTE,
        next_process="CONTRACTNOTEGENERATION",
    )


async def handle_await_contract_note(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """POST file_process_status(CONTRACTNOTEGENERATION) — wait until contract notes complete."""
    poll_state = get_proc(row, "contract_note")
    poll_count = poll_state.get("poll_count", 0) + 1

    result = await cbos.file_process_status(
        segment=row.segment_code,
        process_name="CONTRACTNOTEGENERATION",
        user_id=login_id,
    )
    inc_poll(row, "contract_note", result.response)
    await session.flush()

    if result.is_error:
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "AWAIT_CONTRACT_NOTE",
                "Transient CBOS error — will retry next cycle",
                error=result.error, poll=poll_count,
            ))
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "AWAIT_CONTRACT_NOTE",
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        _fail(row, "CBOS_ERROR", f"CONTRACTNOTEGENERATION error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_pending:
        if poll_count == 1 or poll_count % 5 == 0:
            logger.info(stage_log(
                row.segment_code, "AWAIT_CONTRACT_NOTE",
                "Contract notes not yet generated — waiting",
                response=result.response,
                poll=poll_count,
            ))
        return StageResult.BLOCKED

    logger.info(stage_log(
        row.segment_code, "AWAIT_CONTRACT_NOTE",
        "Contract notes CONFIRMED — segment COMPLETED",
        response=result.response,
        total_polls=poll_count,
        confirmed_at=now.strftime("%H:%M:%S %Z"),
    ))
    mark_stage_done(row, "contract_note", result.response, now)
    _complete(row, now)
    await session.flush()
    return StageResult.COMPLETED


# ---------------------------------------------------------------------------
# Generic confirmation poll (BILLPOSTING / RECON)
# ---------------------------------------------------------------------------

async def _poll_confirmation(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
    process_name: str,
    stage_key: str,
    next_phase: SegmentPhase,
    next_process: str,
) -> StageResult:
    poll_state = get_proc(row, stage_key)
    poll_count = poll_state.get("poll_count", 0) + 1

    result = await cbos.file_process_status(
        segment=row.segment_code,
        process_name=process_name,
        user_id=login_id,
    )
    inc_poll(row, stage_key, result.response)
    await session.flush()

    stage_name = f"AWAIT_{process_name}"

    if result.is_error:
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, stage_name,
                "Transient CBOS error — will retry next cycle",
                error=result.error, poll=poll_count,
            ))
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, stage_name,
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        _fail(row, "CBOS_ERROR", f"{process_name} check error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_pending:
        if poll_count == 1 or poll_count % 5 == 0:
            logger.info(stage_log(
                row.segment_code, stage_name,
                f"{process_name} not yet complete — waiting",
                response=result.response,
                poll=poll_count,
            ))
        return StageResult.BLOCKED

    logger.info(stage_log(
        row.segment_code, stage_name,
        f"{process_name} CONFIRMED — advancing to {next_phase.value}",
        response=result.response,
        total_polls=poll_count,
        confirmed_at=now.strftime("%H:%M:%S %Z"),
    ))
    mark_stage_done(row, stage_key, result.response, now)
    row.current_phase = next_phase
    row.current_process = next_process
    await session.flush()
    return StageResult.ADVANCE


# ---------------------------------------------------------------------------
# Terminal state helpers
# ---------------------------------------------------------------------------

def _fail(row: SegmentExecution, category: str, reason: str, now: datetime) -> None:
    logger.error(stage_log(
        row.segment_code,
        row.current_phase.value if row.current_phase else "UNKNOWN",
        "Stage FAILED — marking segment FAILED",
        category=category,
        reason=reason,
        failed_at=now.strftime("%H:%M:%S %Z"),
    ))
    row.segment_status = SegmentStatus.FAILED
    row.skip_category = category
    row.skip_reason = reason
    row.completed_at = now


def _skip(row: SegmentExecution, category: str, reason: str, now: datetime) -> None:
    logger.info(stage_log(
        row.segment_code,
        row.current_phase.value if row.current_phase else "UNKNOWN",
        "Segment SKIPPED",
        category=category,
        reason=reason,
        skipped_at=now.strftime("%H:%M:%S %Z"),
    ))
    row.segment_status = SegmentStatus.SKIPPED
    row.skip_category = category
    row.skip_reason = reason
    row.current_phase = SegmentPhase.DONE
    row.completed_at = now


def _complete(row: SegmentExecution, now: datetime) -> None:
    logger.info(stage_log(
        row.segment_code, "DONE",
        "Segment fully COMPLETED",
        completed_at=now.strftime("%H:%M:%S %Z"),
    ))
    row.segment_status = SegmentStatus.COMPLETED
    row.current_process = None
    row.current_phase = SegmentPhase.DONE
    row.completed_at = now
