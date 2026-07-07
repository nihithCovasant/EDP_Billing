"""
7-step pipeline stage handlers — identical for all 7 segments (CASH/EQ,
F&O/DR, CD/CUR, SLBM/SL, MCX, NCDEX, MTF). MTF is not special-cased; it is
driven through the exact same handlers as every other segment.

Each handler performs exactly one CBOS API call (Step 2/RESERVE_PID makes
two — get-or-reserve), updates processes_json, mutates the row phase/process
fields, and returns a StageResult.

StageResult values:
  ADVANCE    — stage done; move to next phase in the same cycle
  BLOCKED    — CBOS returned FALSE (not ready) or transient error; come back next cycle
  STOP_NEXT  — trigger fired; start polling on next cycle
  COMPLETED  — all 7 steps done; segment is COMPLETED
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
    record_trigger_attempt,
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


# A stage stuck on a transient CBOS error (timeout, network blip, 5xx) polls
# every wake cycle until it clears or the window deadline hits — logging a
# WARNING on every single one of those cycles is exactly the "log every
# time in the loop" noise to avoid. Throttled the same way as the ordinary
# "still waiting" polls, plus an ERROR escalation if it never clears —
# quiet by default, loud if it looks like a real CBOS outage.
_TRANSIENT_LOG_EVERY_N_POLLS = 5
_TRANSIENT_ESCALATE_AFTER_POLLS = 30


def _log_transient(segment_code: str, stage_name: str, error: str | None, poll_count: int) -> None:
    if poll_count >= _TRANSIENT_ESCALATE_AFTER_POLLS:
        if poll_count % _TRANSIENT_ESCALATE_AFTER_POLLS == 0:
            logger.error(stage_log(
                segment_code, stage_name,
                f"CBOS still failing after {poll_count} consecutive attempts — "
                "likely outage, needs attention",
                error=error, poll=poll_count,
            ))
        return
    if poll_count == 1 or poll_count % _TRANSIENT_LOG_EVERY_N_POLLS == 0:
        logger.warning(stage_log(
            segment_code, stage_name,
            "Transient CBOS error — will retry next cycle",
            error=error, poll=poll_count,
        ))


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
            _log_transient(row.segment_code, "HOLIDAY_CHECK", result.error, poll_count)
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "HOLIDAY_CHECK",
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        await _fail(row, "CBOS_ERROR", f"BeginFileUpload error: {result.error}", now)
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
        await _skip(row, "CBOS_SKIP", "BeginFileUpload returned SKIP — market holiday", now)
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
# Stage 2 — Get or Reserve Process ID
# ---------------------------------------------------------------------------

async def handle_reserve_pid(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """
    Step 2 of the pipeline: always check first whether a Process ID already
    exists for today's segment+date (RPA may have already reserved one), and
    only reserve a new one if none exists yet.

    1. POST getdropdown(EXISTINGPROCESSID) — check for an existing PID.
       - Found  → reuse it, skip straight to AWAIT_FILE_UPLOAD.
       - Not found → fall through to step 2.
    2. POST getNewTradeProcess(PROCESSID="0") — reserve a new PID from CBOS.
    """
    logger.info(stage_log(
        row.segment_code, "RESERVE_PID",
        "Checking for an existing process ID (getdropdown EXISTINGPROCESSID)",
        trade_date=str(row.trade_date),
    ))

    existing = await cbos.get_existing_process_id(
        segment=row.segment_code,
        login_id=login_id,
        trade_date=row.trade_date,
    )

    if existing.found and existing.process_id:
        logger.info(stage_log(
            row.segment_code, "RESERVE_PID",
            "Existing process ID found — reusing it, skipping reservation",
            pid=existing.process_id,
            desc=existing.description,
        ))
        return await _pid_resolved(row, session, existing.process_id, "EXISTING", now)

    if existing.error and existing.is_transient:
        logger.warning(stage_log(
            row.segment_code, "RESERVE_PID",
            "Transient CBOS error on getdropdown(EXISTINGPROCESSID) — will retry next cycle",
            error=existing.error,
        ))
        return StageResult.BLOCKED

    logger.info(stage_log(
        row.segment_code, "RESERVE_PID",
        "No existing process ID — reserving a new one (PROCESSID=0)",
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
        await _fail(row, "CBOS_ERROR", f"getNewTradeProcess(PROCESSID=0) failed: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    return await _pid_resolved(row, session, result.process_id, "RESERVED_NEW", now)


async def _pid_resolved(
    row: SegmentExecution,
    session: AsyncSession,
    process_id: str,
    source: str,
    now: datetime,
) -> StageResult:
    """Shared bookkeeping once Step 2 resolves a process_id, either way."""
    row.process_id = process_id
    row.process_id_reserved_at = now
    set_proc(row, "trigger", {
        "status": "PID_RESERVED",
        "process_id_reserved": process_id,
        "process_id_source": source,
        "reserved_at": now.isoformat(),
    })
    row.current_phase = SegmentPhase.AWAIT_FILE_UPLOAD
    row.current_process = "FILEUPLOAD"
    await session.flush()

    logger.info(stage_log(
        row.segment_code, "RESERVE_PID",
        f"Process ID resolved ({source}) — proceeding to AWAIT_FILE_UPLOAD",
        pid=process_id,
        source=source,
        resolved_at=now.strftime("%H:%M:%S %Z"),
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
            _log_transient(row.segment_code, "AWAIT_FILE_UPLOAD", result.error, poll_count)
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "AWAIT_FILE_UPLOAD",
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        await _fail(row, "CBOS_ERROR", f"FILEUPLOAD check error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_skip:
        logger.info(stage_log(
            row.segment_code, "AWAIT_FILE_UPLOAD",
            "CBOS returned SKIP for FILEUPLOAD — segment will be SKIPPED",
            response=result.response, poll=poll_count,
        ))
        await _skip(row, "CBOS_SKIP", "FILEUPLOAD returned SKIP", now)
        await session.flush()
        return StageResult.SKIPPED

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

    Double-trigger protection: "TRIGGERING" is committed to processes_json
    BEFORE the CBOS call is made, so the DB always leads the call. If the
    pod dies before the eventual record_trigger()/record_trigger_failed()
    write, the next cycle re-enters with status still "TRIGGERING" and
    runs _recover_trigger() instead of blindly firing again.
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
            await _fail(row, "CBOS_ERROR", "No process_id available for trigger", now)
            await session.flush()
            return StageResult.FAILED

    if get_proc(row, "trigger").get("status") == "TRIGGERING":
        return await _recover_trigger(cbos, row, session, login_id, now)

    # First attempt for this segment-day — commit the pre-commit marker
    # BEFORE calling CBOS. Must be commit(), not flush(): flush() alone
    # stays inside the outer (uncommitted) transaction, so a crash before
    # the enclosing session commits would roll back "TRIGGERING" along
    # with it — the exact thing this marker exists to survive.
    # expire_on_commit=False keeps `row`'s loaded attributes usable after.
    record_trigger_attempt(row, now)
    await session.commit()

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
    return await _finalize_trigger_call(row, session, result, now)


async def _recover_trigger(
    cbos: CbosClient,
    row: SegmentExecution,
    session: AsyncSession,
    login_id: str,
    now: datetime,
) -> StageResult:
    """
    Recovery for a segment resuming with trigger.status == "TRIGGERING" —
    we intended to fire but never durably learned if CBOS received it.
    Checks CBOS's Table2 step statuses on the saved PROCESSID: any step
    IN_PROGRESS/SUCCESS means CBOS already has it (catch DB up to
    TRIGGERED, don't re-fire); all PENDING means safe to trigger now.
    """
    logger.warning(stage_log(
        row.segment_code, "TRIGGER",
        "Resuming with an unconfirmed trigger attempt — checking CBOS "
        "before deciding whether to re-trigger",
        pid=row.process_id,
    ))
    check = await cbos.get_new_trade_process(
        group_name=row.segment_code,
        login_id=login_id,
        trade_date=row.trade_date,
        process_id=row.process_id,
    )
    if not check.success:
        if check.is_transient:
            logger.warning(stage_log(
                row.segment_code, "TRIGGER",
                "Transient CBOS error while checking recovery state — will retry next cycle",
                pid=row.process_id,
                error=check.error,
            ))
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "TRIGGER",
            "Permanent CBOS error while checking recovery state — marking FAILED",
            pid=row.process_id,
            error=check.error,
        ))
        record_trigger_failed(row, check.error or "RECOVERY_CHECK_FAILED", now)
        await _fail(row, "CBOS_ERROR", f"Trigger recovery check failed: {check.error}", now)
        await session.flush()
        return StageResult.FAILED

    already_running = any(
        (step.status or "").upper() in ("IN_PROGRESS", "SUCCESS")
        for step in check.steps
    )
    if already_running:
        logger.info(stage_log(
            row.segment_code, "TRIGGER",
            "CBOS already received/executing the trigger — NOT re-triggering; "
            "catching DB up to TRIGGERED",
            pid=row.process_id,
            steps=[f"{s.name}:{s.status}" for s in check.steps],
        ))
        return await _finalize_trigger_success(row, session, row.process_id, check.is_runnable, now)

    logger.info(stage_log(
        row.segment_code, "TRIGGER",
        "CBOS never received the trigger (all steps PENDING) — safe to re-trigger",
        pid=row.process_id,
    ))
    result = await cbos.get_new_trade_process(
        group_name=row.segment_code,
        login_id=login_id,
        trade_date=row.trade_date,
        process_id=row.process_id,
    )
    return await _finalize_trigger_call(row, session, result, now)


async def _finalize_trigger_call(
    row: SegmentExecution,
    session: AsyncSession,
    result,
    now: datetime,
) -> StageResult:
    """Shared success/failure handling for a getNewTradeProcess trigger-mode call."""
    if not result.success:
        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "TRIGGER",
                "Transient CBOS error — leaving TRIGGERING; will re-check next cycle",
                pid=row.process_id,
                error=result.error,
            ))
            # Deliberately do NOT write processes_json here — it must stay
            # "TRIGGERING" so the next cycle goes through _recover_trigger()
            # instead of blindly re-firing.
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "TRIGGER",
            "Trigger FAILED — marking segment FAILED",
            pid=row.process_id,
            error=result.error,
        ))
        record_trigger_failed(row, result.error or "TRIGGER_FAILED", now)
        await _fail(
            row, "CBOS_ERROR",
            f"getNewTradeProcess(PROCESSID={row.process_id}) failed: {result.error}", now
        )
        await session.flush()
        return StageResult.FAILED

    return await _finalize_trigger_success(row, session, row.process_id, result.is_runnable, now)


async def _finalize_trigger_success(
    row: SegmentExecution,
    session: AsyncSession,
    process_id: str,
    is_runnable: bool,
    now: datetime,
) -> StageResult:
    """Common "trigger confirmed" bookkeeping, shared by the normal path and both recovery branches."""
    record_trigger(row, process_id, is_runnable, now)
    row.current_phase = SegmentPhase.AWAIT_BILLPOSTING
    row.current_process = "BILLPOSTING"
    await session.flush()

    logger.info(stage_log(
        row.segment_code, "TRIGGER",
        "Process TRIGGERED successfully — will poll BILLPOSTING next cycle",
        pid=process_id,
        is_runnable=is_runnable,
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
            _log_transient(row.segment_code, "AWAIT_CONTRACT_NOTE", result.error, poll_count)
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, "AWAIT_CONTRACT_NOTE",
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        await _fail(row, "CBOS_ERROR", f"CONTRACTNOTEGENERATION error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_skip:
        logger.info(stage_log(
            row.segment_code, "AWAIT_CONTRACT_NOTE",
            "CBOS returned SKIP for CONTRACTNOTEGENERATION — segment will be SKIPPED",
            response=result.response, poll=poll_count,
        ))
        await _skip(row, "CBOS_SKIP", "CONTRACTNOTEGENERATION returned SKIP", now)
        await session.flush()
        return StageResult.SKIPPED

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
            _log_transient(row.segment_code, stage_name, result.error, poll_count)
            return StageResult.BLOCKED
        logger.error(stage_log(
            row.segment_code, stage_name,
            "Permanent CBOS error — marking FAILED",
            error=result.error,
        ))
        await _fail(row, "CBOS_ERROR", f"{process_name} check error: {result.error}", now)
        await session.flush()
        return StageResult.FAILED

    if result.is_skip:
        logger.info(stage_log(
            row.segment_code, stage_name,
            f"CBOS returned SKIP for {process_name} — segment will be SKIPPED",
            response=result.response, poll=poll_count,
        ))
        await _skip(row, "CBOS_SKIP", f"{process_name} returned SKIP", now)
        await session.flush()
        return StageResult.SKIPPED

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

async def _fail(
    row: SegmentExecution, category: str, reason: str, now: datetime,
) -> None:
    """
    Mark the segment FAILED — a permanent error. Halts the rest of the
    day's sequential chain (orchestrator stops at the first FAILED
    segment), reserved for real errors, not timeouts (see _skip).
    """
    logger.error(stage_log(
        row.segment_code,
        row.current_phase.value if row.current_phase else "UNKNOWN",
        "Stage FAILED — marking segment FAILED (halts today's remaining chain)",
        category=category,
        reason=reason,
        failed_at=now.strftime("%H:%M:%S %Z"),
    ))
    row.segment_status = SegmentStatus.FAILED
    row.skip_category = category
    row.skip_reason = reason
    row.completed_at = now


async def _skip(
    row: SegmentExecution, category: str, reason: str, now: datetime,
) -> None:
    """
    Mark the segment SKIPPED (holiday, CBOS_SKIP at any stage, or TIMEOUT).
    Unlike FAILED, does NOT halt the chain — orchestrator moves on.
    """
    logger.info(stage_log(
        row.segment_code,
        row.current_phase.value if row.current_phase else "UNKNOWN",
        "Segment SKIPPED — continuing to next segment in sequence",
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
