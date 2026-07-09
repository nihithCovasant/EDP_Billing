"""
Shared base for all 10 real-segment state machines (CASH/EQ, F&O/DR, CD/CUR,
SLB, NCDEX, NCDEXPHY, MCX, MCXPHY, NSECOM, MF) — none are special-cased, so
every one of the 10 files under segments/ is a ~5-line subclass that just
sets SEGMENT_CODE; all step logic lives once, here.

Ported from the old pipeline/stages.py free functions — same CBOS calls,
same processes_json bookkeeping, same crash-recovery logic for TRIGGER —
just returning a SegmentHandlerResult instead of mutating row.current_phase
directly and returning a bare StageResult. AbstractStateMachine.update_state()
is what actually applies next_phase/next_process/next_status to the row.

7-step pipeline:
  HOLIDAY_CHECK -> RESERVE_PID -> AWAIT_FILE_UPLOAD -> TRIGGER ->
  AWAIT_BILLPOSTING -> AWAIT_RECON -> AWAIT_CONTRACT_NOTE -> (COMPLETED)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SegmentExecution, SegmentPhase
from ..utils.json_helpers import (
    get_proc,
    inc_poll,
    mark_stage_done,
    record_trigger,
    record_trigger_attempt,
    record_trigger_failed,
    set_proc,
)
from ..utils.log_fmt import stage_log
from .AbstractStateMachine import AbstractSegmentStateMachine
from .SegmentHandlerResult import ADVANCE, BLOCKED, STOP_NEXT, SegmentHandlerResult
from .SegmentTransitionMap import REAL_SEGMENT_TRANSITION_MAP
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


class RealSegmentStateMachine(AbstractSegmentStateMachine):
    def __init__(self) -> None:
        super().__init__(REAL_SEGMENT_TRANSITION_MAP)

    def get_state_handler(self, phase: SegmentPhase | None):
        handlers = {
            SegmentPhase.HOLIDAY_CHECK: self.handle_holiday_check,
            SegmentPhase.RESERVE_PID: self.handle_reserve_pid,
            SegmentPhase.AWAIT_FILE_UPLOAD: self.handle_await_file_upload,
            SegmentPhase.TRIGGER: self.handle_trigger,
            SegmentPhase.AWAIT_BILLPOSTING: self.handle_await_billposting,
            SegmentPhase.AWAIT_RECON: self.handle_await_recon,
            SegmentPhase.AWAIT_CONTRACT_NOTE: self.handle_await_contract_note,
        }
        return handlers.get(phase)

    # ---------------------------------------------------------------
    # Stage 1 — Holiday Check
    # ---------------------------------------------------------------

    async def handle_holiday_check(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(BeginFileUpload). SKIP -> holiday | FALSE -> not yet open | TRUE -> proceed."""
        logger.info(stage_log(row.segment_code, "HOLIDAY_CHECK", "Checking holiday gate (BeginFileUpload)"))

        result = await cbos.file_process_status(
            segment=row.segment_code, process_name="BeginFileUpload", user_id=login_id,
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
                    error=result.error, poll=poll_count,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "HOLIDAY_CHECK",
                "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"BeginFileUpload error: {result.error}", now)

        if result.is_skip:
            logger.info(stage_log(
                row.segment_code, "HOLIDAY_CHECK",
                "Market HOLIDAY — segment will be SKIPPED",
                response=result.response, at=now.strftime("%H:%M:%S %Z"),
            ))
            mark_stage_done(row, "holiday_check", result.response, now)
            return self._skip_result(row, "CBOS_SKIP", "BeginFileUpload returned SKIP — market holiday", now)

        if result.is_pending:
            logger.info(stage_log(
                row.segment_code, "HOLIDAY_CHECK",
                "EDP window not yet open — will check next cycle",
                response=result.response, poll=poll_count,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        logger.info(stage_log(
            row.segment_code, "HOLIDAY_CHECK",
            "Holiday check PASSED — proceeding to RESERVE_PID",
            response=result.response, at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_stage_done(row, "holiday_check", result.response, now)
        return SegmentHandlerResult(outcome=ADVANCE, next_phase=SegmentPhase.RESERVE_PID, next_process=None)

    # ---------------------------------------------------------------
    # Stage 2 — Get or Reserve Process ID
    # ---------------------------------------------------------------

    async def handle_reserve_pid(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """
        1. POST getdropdown(EXISTINGPROCESSID) — reuse an existing PID if RPA
           already reserved one.
        2. Else POST getNewTradeProcess(PROCESSID="0") — reserve a new PID.
        """
        logger.info(stage_log(
            row.segment_code, "RESERVE_PID",
            "Checking for an existing process ID (getdropdown EXISTINGPROCESSID)",
            trade_date=str(row.trade_date),
        ))

        existing = await cbos.get_existing_process_id(
            segment=row.segment_code, login_id=login_id, trade_date=row.trade_date,
        )

        if existing.found and existing.process_id:
            logger.info(stage_log(
                row.segment_code, "RESERVE_PID",
                "Existing process ID found — reusing it, skipping reservation",
                pid=existing.process_id, desc=existing.description,
            ))
            return self._pid_resolved(row, existing.process_id, "EXISTING", now)

        if existing.error and existing.is_transient:
            logger.warning(stage_log(
                row.segment_code, "RESERVE_PID",
                "Transient CBOS error on getdropdown(EXISTINGPROCESSID) — will retry next cycle",
                error=existing.error,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        logger.info(stage_log(
            row.segment_code, "RESERVE_PID",
            "No existing process ID — reserving a new one (PROCESSID=0)",
            trade_date=str(row.trade_date),
        ))

        result = await cbos.get_new_trade_process(
            group_name=row.segment_code, login_id=login_id, trade_date=row.trade_date, process_id="0",
        )

        if not result.success or not result.process_id:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "RESERVE_PID",
                    "Transient CBOS error — will retry next cycle", error=result.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "RESERVE_PID",
                "Failed to allocate process ID — marking FAILED", error=result.error,
            ))
            return self._fail_result(
                row, "CBOS_ERROR", f"getNewTradeProcess(PROCESSID=0) failed: {result.error}", now,
            )

        return self._pid_resolved(row, result.process_id, "RESERVED_NEW", now)

    def _pid_resolved(
        self, row: SegmentExecution, process_id: str, source: str, now: datetime,
    ) -> SegmentHandlerResult:
        """Shared bookkeeping once Step 2 resolves a process_id, either way."""
        row.process_id = process_id
        row.process_id_reserved_at = now
        set_proc(row, "trigger", {
            "status": "PID_RESERVED",
            "process_id_reserved": process_id,
            "process_id_source": source,
            "reserved_at": now.isoformat(),
        })

        logger.info(stage_log(
            row.segment_code, "RESERVE_PID",
            f"Process ID resolved ({source}) — proceeding to AWAIT_FILE_UPLOAD",
            pid=process_id, source=source, resolved_at=now.strftime("%H:%M:%S %Z"),
        ))
        return SegmentHandlerResult(
            outcome=ADVANCE, next_phase=SegmentPhase.AWAIT_FILE_UPLOAD, next_process="FILEUPLOAD",
        )

    # ---------------------------------------------------------------
    # Stage 3 — Await File Upload
    # ---------------------------------------------------------------

    async def handle_await_file_upload(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(FILEUPLOAD) — poll until exchange files are uploaded."""
        poll_state = get_proc(row, "file_upload_ready")
        poll_count = poll_state.get("poll_count", 0) + 1

        result = await cbos.file_process_status(
            segment=row.segment_code, process_name="FILEUPLOAD", user_id=login_id,
        )
        inc_poll(row, "file_upload_ready", result.response)
        await session.flush()

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "AWAIT_FILE_UPLOAD",
                    "Transient CBOS error — will retry next cycle", error=result.error, poll=poll_count,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "AWAIT_FILE_UPLOAD",
                "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"FILEUPLOAD check error: {result.error}", now)

        if result.is_skip:
            logger.info(stage_log(
                row.segment_code, "AWAIT_FILE_UPLOAD",
                "CBOS returned SKIP for FILEUPLOAD — segment will be SKIPPED",
                response=result.response, poll=poll_count,
            ))
            return self._skip_result(row, "CBOS_SKIP", "FILEUPLOAD returned SKIP", now)

        if result.is_pending:
            if poll_count == 1 or poll_count % 5 == 0:
                logger.info(stage_log(
                    row.segment_code, "AWAIT_FILE_UPLOAD",
                    "Exchange files not yet uploaded — waiting", response=result.response, poll=poll_count,
                ))
            return SegmentHandlerResult(outcome=BLOCKED)

        logger.info(stage_log(
            row.segment_code, "AWAIT_FILE_UPLOAD",
            "All exchange files uploaded — proceeding to TRIGGER",
            response=result.response, total_polls=poll_count, ready_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_stage_done(row, "file_upload_ready", result.response, now)
        return SegmentHandlerResult(outcome=ADVANCE, next_phase=SegmentPhase.TRIGGER, next_process=None)

    # ---------------------------------------------------------------
    # Stage 4 — Trigger Processing
    # ---------------------------------------------------------------

    async def handle_trigger(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """
        POST getNewTradeProcess(PROCESSID=<actual>) — starts billing/calculation.

        Double-trigger protection: "TRIGGERING" is committed to
        processes_json BEFORE the CBOS call is made, so the DB always
        leads the call. If the pod dies before the eventual
        record_trigger()/record_trigger_failed() write, the next cycle
        re-enters with status still "TRIGGERING" and runs
        _recover_trigger() instead of blindly firing again.
        """
        if not row.process_id:
            logger.warning(stage_log(
                row.segment_code, "TRIGGER",
                "process_id missing — attempting crash recovery from CBOS",
                trade_date=str(row.trade_date),
            ))
            recovery = await cbos.get_existing_process_id(
                segment=row.segment_code, login_id=login_id, trade_date=row.trade_date,
            )
            if recovery.found and recovery.process_id:
                row.process_id = recovery.process_id
                logger.info(stage_log(
                    row.segment_code, "TRIGGER", "process_id recovered from CBOS",
                    pid=recovery.process_id, desc=recovery.description,
                ))
            else:
                logger.error(stage_log(
                    row.segment_code, "TRIGGER",
                    "Cannot recover process_id — marking FAILED", error=recovery.error,
                ))
                return self._fail_result(row, "CBOS_ERROR", "No process_id available for trigger", now)

        if get_proc(row, "trigger").get("status") == "TRIGGERING":
            return await self._recover_trigger(cbos, row, session, login_id, now)

        # First attempt for this segment-day — commit the pre-commit marker
        # BEFORE calling CBOS. Must be commit(), not flush(): flush() alone
        # stays inside the outer (uncommitted) transaction, so a crash
        # before the enclosing session commits would roll back
        # "TRIGGERING" along with it — the exact thing this marker exists
        # to survive. expire_on_commit=False keeps `row` usable after.
        record_trigger_attempt(row, now)
        await session.commit()

        logger.info(stage_log(
            row.segment_code, "TRIGGER", "Firing process trigger (getNewTradeProcess)",
            pid=row.process_id, trade_date=str(row.trade_date), triggered_at=now.strftime("%H:%M:%S %Z"),
        ))

        result = await cbos.get_new_trade_process(
            group_name=row.segment_code, login_id=login_id, trade_date=row.trade_date, process_id=row.process_id,
        )
        return await self._finalize_trigger_call(row, result, now)

    async def _recover_trigger(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """
        Resuming with trigger.status == "TRIGGERING" — checks CBOS's
        Table2 step statuses on the saved PROCESSID: any step
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
            group_name=row.segment_code, login_id=login_id, trade_date=row.trade_date, process_id=row.process_id,
        )
        if not check.success:
            if check.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "TRIGGER",
                    "Transient CBOS error while checking recovery state — will retry next cycle",
                    pid=row.process_id, error=check.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "TRIGGER",
                "Permanent CBOS error while checking recovery state — marking FAILED",
                pid=row.process_id, error=check.error,
            ))
            record_trigger_failed(row, check.error or "RECOVERY_CHECK_FAILED", now)
            return self._fail_result(row, "CBOS_ERROR", f"Trigger recovery check failed: {check.error}", now)

        already_running = any(
            (step.status or "").upper() in ("IN_PROGRESS", "SUCCESS") for step in check.steps
        )
        if already_running:
            logger.info(stage_log(
                row.segment_code, "TRIGGER",
                "CBOS already received/executing the trigger — NOT re-triggering; "
                "catching DB up to TRIGGERED",
                pid=row.process_id, steps=[f"{s.name}:{s.status}" for s in check.steps],
            ))
            return self._finalize_trigger_success(row, row.process_id, check.is_runnable, now)

        logger.info(stage_log(
            row.segment_code, "TRIGGER",
            "CBOS never received the trigger (all steps PENDING) — safe to re-trigger",
            pid=row.process_id,
        ))
        result = await cbos.get_new_trade_process(
            group_name=row.segment_code, login_id=login_id, trade_date=row.trade_date, process_id=row.process_id,
        )
        return await self._finalize_trigger_call(row, result, now)

    async def _finalize_trigger_call(
        self, row: SegmentExecution, result, now: datetime,
    ) -> SegmentHandlerResult:
        """Shared success/failure handling for a getNewTradeProcess trigger-mode call."""
        if not result.success:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "TRIGGER",
                    "Transient CBOS error — leaving TRIGGERING; will re-check next cycle",
                    pid=row.process_id, error=result.error,
                ))
                # Deliberately do NOT write processes_json here — it must
                # stay "TRIGGERING" so the next cycle goes through
                # _recover_trigger() instead of blindly re-firing.
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "TRIGGER", "Trigger FAILED — marking segment FAILED",
                pid=row.process_id, error=result.error,
            ))
            record_trigger_failed(row, result.error or "TRIGGER_FAILED", now)
            return self._fail_result(
                row, "CBOS_ERROR",
                f"getNewTradeProcess(PROCESSID={row.process_id}) failed: {result.error}", now,
            )

        return self._finalize_trigger_success(row, row.process_id, result.is_runnable, now)

    def _finalize_trigger_success(
        self, row: SegmentExecution, process_id: str, is_runnable: bool, now: datetime,
    ) -> SegmentHandlerResult:
        """Common "trigger confirmed" bookkeeping, shared by the normal path and both recovery branches."""
        record_trigger(row, process_id, is_runnable, now)

        logger.info(stage_log(
            row.segment_code, "TRIGGER", "Process TRIGGERED successfully — will poll BILLPOSTING next cycle",
            pid=process_id, is_runnable=is_runnable, triggered_at=now.strftime("%H:%M:%S %Z"),
        ))
        return SegmentHandlerResult(
            outcome=STOP_NEXT, next_phase=SegmentPhase.AWAIT_BILLPOSTING, next_process="BILLPOSTING",
        )

    # ---------------------------------------------------------------
    # Stages 5 / 6 / 7 — Poll CBOS completion status
    # ---------------------------------------------------------------

    async def handle_await_billposting(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(BILLPOSTING) — wait until billing calculations complete."""
        return await self._poll_confirmation(
            cbos, row, session, login_id, now,
            process_name="BILLPOSTING", stage_key="bill_posting",
            next_phase=SegmentPhase.AWAIT_RECON, next_process="RECON",
        )

    async def handle_await_recon(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(RECON) — wait until reconciliation completes."""
        return await self._poll_confirmation(
            cbos, row, session, login_id, now,
            process_name="RECON", stage_key="recon",
            next_phase=SegmentPhase.AWAIT_CONTRACT_NOTE, next_process="CONTRACTNOTEGENERATION",
        )

    async def handle_await_contract_note(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(CONTRACTNOTEGENERATION) — wait until contract notes complete."""
        poll_state = get_proc(row, "contract_note")
        poll_count = poll_state.get("poll_count", 0) + 1

        result = await cbos.file_process_status(
            segment=row.segment_code, process_name="CONTRACTNOTEGENERATION", user_id=login_id,
        )
        inc_poll(row, "contract_note", result.response)
        await session.flush()

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "AWAIT_CONTRACT_NOTE",
                    "Transient CBOS error — will retry next cycle", error=result.error, poll=poll_count,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "AWAIT_CONTRACT_NOTE",
                "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"CONTRACTNOTEGENERATION error: {result.error}", now)

        if result.is_skip:
            logger.info(stage_log(
                row.segment_code, "AWAIT_CONTRACT_NOTE",
                "CBOS returned SKIP for CONTRACTNOTEGENERATION — segment will be SKIPPED",
                response=result.response, poll=poll_count,
            ))
            return self._skip_result(row, "CBOS_SKIP", "CONTRACTNOTEGENERATION returned SKIP", now)

        if result.is_pending:
            if poll_count == 1 or poll_count % 5 == 0:
                logger.info(stage_log(
                    row.segment_code, "AWAIT_CONTRACT_NOTE",
                    "Contract notes not yet generated — waiting", response=result.response, poll=poll_count,
                ))
            return SegmentHandlerResult(outcome=BLOCKED)

        logger.info(stage_log(
            row.segment_code, "AWAIT_CONTRACT_NOTE",
            "Contract notes CONFIRMED — segment COMPLETED",
            response=result.response, total_polls=poll_count, confirmed_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_stage_done(row, "contract_note", result.response, now)
        return self._complete_result(row, now)

    async def _poll_confirmation(
        self,
        cbos: CbosClient,
        row: SegmentExecution,
        session: AsyncSession,
        login_id: str,
        now: datetime,
        process_name: str,
        stage_key: str,
        next_phase: SegmentPhase,
        next_process: str,
    ) -> SegmentHandlerResult:
        poll_state = get_proc(row, stage_key)
        poll_count = poll_state.get("poll_count", 0) + 1

        result = await cbos.file_process_status(
            segment=row.segment_code, process_name=process_name, user_id=login_id,
        )
        inc_poll(row, stage_key, result.response)
        await session.flush()

        stage_name = f"AWAIT_{process_name}"

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, stage_name,
                    "Transient CBOS error — will retry next cycle", error=result.error, poll=poll_count,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, stage_name, "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"{process_name} check error: {result.error}", now)

        if result.is_skip:
            logger.info(stage_log(
                row.segment_code, stage_name,
                f"CBOS returned SKIP for {process_name} — segment will be SKIPPED",
                response=result.response, poll=poll_count,
            ))
            return self._skip_result(row, "CBOS_SKIP", f"{process_name} returned SKIP", now)

        if result.is_pending:
            if poll_count == 1 or poll_count % 5 == 0:
                logger.info(stage_log(
                    row.segment_code, stage_name,
                    f"{process_name} not yet complete — waiting", response=result.response, poll=poll_count,
                ))
            return SegmentHandlerResult(outcome=BLOCKED)

        logger.info(stage_log(
            row.segment_code, stage_name, f"{process_name} CONFIRMED — advancing to {next_phase.value}",
            response=result.response, total_polls=poll_count, confirmed_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_stage_done(row, stage_key, result.response, now)
        return SegmentHandlerResult(outcome=ADVANCE, next_phase=next_phase, next_process=next_process)
