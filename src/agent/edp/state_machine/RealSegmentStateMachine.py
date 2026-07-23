"""
Shared base for all 9 real-segment state machines (CASH/EQ, F&O/DR, CD/CUR,
SLB, NCDEX, NCDEXPHY, MCX, MCXPHY, NSECOM) — none are special-cased, so
every one of the 9 files under segments/ is a ~5-line subclass that just
sets SEGMENT_CODE; all step logic lives once, here.

flow states (no "phases" — see models.SegmentState):
  INIT -> [DOWNLOADING -> UPLOADING ->] WAITING_FOR_FILE_UPLOAD -> TRIGGERED
  -> WAITING_FOR_BILLPOSTING -> WAITING_FOR_RECON ->
  WAITING_FOR_CONTRACT_NOTE_GENERATION -> (SUCCEEDED)

DOWNLOADING/UPLOADING (engine-owned saga, BATCH_HANDOFF_CONTRACT.md) are
taken only by config.download_segments (MCX + EQ today): DOWNLOADING asks
the RPA bot for the full-segment download (the bot finalizes a checksummed
manifest), UPLOADING hands that manifest to the uploader's POST /batches;
then the unchanged FILEUPLOAD wait takes over. The bot and uploader own
their own retries/idempotency; these handlers only sequence them.

INIT's handler does the holiday-check operation; WAITING_FOR_FILE_UPLOAD's
handler READS the process ID on its first entries (no process_id yet,
recorded as a "reserve_process_id" step nested inside its own
processes_json["WAITING_FOR_FILE_UPLOAD"]["steps"] dict), then polls
FILEUPLOAD on every later entry — neither "holiday check" nor "resolve
process id" is its own state, both are operations folded into the state
that owns them, per the happy-flow tables.

PROCESSID ownership (see EDPBilling_FIle_Upload/docs/CBOS_HANDOFF_CONTRACT.md):
the UPLOADER is the sole reserver — it mints (or reuses) the PID as part of
uploading the exchange files. This agent only ever READS the PID back via
getdropdown(EXISTINGPROCESSID); "no PID yet" simply means the uploader
hasn't gotten there yet and is a normal wait, never a reason to mint. The
old fallback that reserved via getNewTradeProcess(PROCESSID="0") was the
dual-writer race behind the 2026-07-21 PID-mismatch incident and was
removed deliberately — do not reintroduce it. Every processes_json top-level
key is exactly a SegmentState.value string (see json_helpers.py's module
docstring).
TRIGGERED is the one genuine crash-safety-critical wait in this pipeline.
WAITING_FOR_BILLPOSTING/_RECON/_CONTRACT_NOTE_GENERATION are pure polls —
CBOS auto-runs each step once TRIGGERED fires, the agent only observes.

Each handler call does exactly one action and returns — AbstractStateMachine
applies the resulting single state transition; there is no internal loop.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..edpb_client import get_edpb_client
from ..models import SegmentExecution, SegmentState
from ..utils.json_helpers import (
    get_download_result,
    get_state,
    mark_step_done,
    set_state,
    record_download_result,
    record_pid_reservation,
    record_poll,
    record_trigger,
    record_trigger_attempt,
    record_trigger_failed,
)
from ..utils.log_fmt import stage_log
from .AbstractStateMachine import AbstractSegmentStateMachine
from .SegmentHandlerResult import ADVANCE, BLOCKED, SegmentHandlerResult
from .TradeSegmentTransitionFactory import REAL_SEGMENT_TRANSITION_MAP
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


class RealSegmentStateMachine(AbstractSegmentStateMachine):
    def __init__(self) -> None:
        super().__init__(REAL_SEGMENT_TRANSITION_MAP)

    def get_state_handler(self, state: SegmentState | None):
        handlers = {
            SegmentState.INIT: self.handle_init,
            SegmentState.DOWNLOADING: self.handle_downloading,
            SegmentState.UPLOADING: self.handle_uploading,
            SegmentState.WAITING_FOR_FILE_UPLOAD: self.handle_waiting_for_file_upload,
            SegmentState.TRIGGERED: self.handle_triggered,
            SegmentState.WAITING_FOR_BILLPOSTING: self.handle_waiting_for_billposting,
            SegmentState.WAITING_FOR_RECON: self.handle_waiting_for_recon,
            SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION: self.handle_waiting_for_contract_note_generation,
        }
        return handlers.get(state)

    # ---------------------------------------------------------------
    # INIT — operation: holiday check
    # ---------------------------------------------------------------

    async def handle_init(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(BeginFileUpload). SKIP -> holiday | FALSE -> not yet open | TRUE -> proceed."""
        logger.info(stage_log(row.segment_code, "INIT", "Checking holiday gate (BeginFileUpload)"))

        result = await cbos.file_process_status(
            segment=row.segment_code, process_name="BeginFileUpload", user_id=login_id,
        )
        record_poll(row, SegmentState.INIT.value, "BeginFileUpload_STATUS", result.response, now)
        await session.flush()

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "INIT",
                    "Transient CBOS error — will retry next cycle", error=result.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "INIT",
                "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"BeginFileUpload error: {result.error}", now)

        if result.is_skip:
            logger.info(stage_log(
                row.segment_code, "INIT",
                "Market HOLIDAY — segment will be SKIPPED",
                response=result.response, at=now.strftime("%H:%M:%S %Z"),
            ))
            mark_step_done(row, SegmentState.INIT.value, "BeginFileUpload_STATUS", result.response, now)
            return self._skip_result(row, "CBOS_SKIP", "BeginFileUpload returned SKIP — market holiday", now)

        if result.is_pending:
            logger.info(stage_log(
                row.segment_code, "INIT",
                "EDP window not yet open — will check next cycle", response=result.response,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        # Engine-owned saga (BATCH_HANDOFF_CONTRACT.md): segments the RPA bot
        # can download route through DOWNLOADING -> UPLOADING first; everyone
        # else waits for files exactly as before.
        next_state = (
            SegmentState.DOWNLOADING
            if self._is_download_segment(row.segment_code)
            else SegmentState.WAITING_FOR_FILE_UPLOAD
        )
        logger.info(stage_log(
            row.segment_code, "INIT",
            f"Holiday check PASSED — proceeding to {next_state.value}",
            response=result.response, at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_step_done(row, SegmentState.INIT.value, "BeginFileUpload_STATUS", result.response, now)
        return SegmentHandlerResult(outcome=ADVANCE, next_state=next_state, next_process=None)

    @staticmethod
    def _is_download_segment(segment_code: str) -> bool:
        """True when this segment's downloads are engine-driven (config
        download_segments AND the client actually has a route for it)."""
        from ..config import load_edp_config
        from ..edpb_client import EdpbClient

        cfg = load_edp_config()
        return (
            segment_code.upper() in cfg.download_segments
            and EdpbClient.supports_segment(segment_code)
        )

    # ---------------------------------------------------------------
    # DOWNLOADING — call the RPA bot's full-segment download; the bot
    # finalizes a checksummed manifest and answers with its path.
    # UPLOADING — hand that manifest to the uploader's POST /batches.
    # ---------------------------------------------------------------

    @staticmethod
    def _run_correlation_id(row: SegmentExecution) -> str:
        """One correlation id per (segment, trade date) run, minted on the
        first DOWNLOADING entry and persisted in processes_json so every
        later call — download, submit, batch-status — and every service's
        log (engine, bot via X-Request-ID, uploader via the manifest and its
        audit rows) carries the SAME id. One grep traces the whole journey."""
        state = get_state(row, SegmentState.DOWNLOADING.value)
        cid = state.get("correlation_id")
        if not cid:
            cid = f"edp-{row.segment_code.lower()}-{row.trade_date.isoformat()}-{uuid.uuid4().hex[:8]}"
            state["correlation_id"] = cid
            set_state(row, SegmentState.DOWNLOADING.value, state)
        return cid

    async def handle_downloading(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """One bot call per entry. success/partial -> record manifest+batch_id,
        ADVANCE to UPLOADING (partial still advances: the uploader's
        completeness gate is the authority — the bot/engine only report).
        no_data -> files not published yet, wait. failed/error -> bounded
        attempts, then FAILED. Crash-safe by construction: re-running the
        download supersedes the manifest (fresh batch_id), never duplicates."""
        from ..config import load_edp_config

        client = get_edpb_client()
        logger.info(stage_log(
            row.segment_code, "DOWNLOADING",
            "Requesting full-segment download from the RPA bot",
            trade_date=str(row.trade_date),
        ))
        result = await client.request_download(
            row.segment_code, row.trade_date, correlation_id=self._run_correlation_id(row),
        )
        record_poll(row, SegmentState.DOWNLOADING.value, "edpb_download",
                    f"{result.status}: {result.message[:200]}", now)
        await session.flush()

        if result.status in ("success", "partial") and result.manifest_path and result.batch_id:
            record_download_result(row, result.manifest_path, result.batch_id, result.status, now)
            logger.info(stage_log(
                row.segment_code, "DOWNLOADING",
                f"Download {result.status} — manifest finalized, proceeding to UPLOADING",
                batch_id=result.batch_id, manifest=result.manifest_path,
            ))
            return SegmentHandlerResult(outcome=ADVANCE, next_state=SegmentState.UPLOADING, next_process=None)

        if result.status == "no_data":
            logger.info(stage_log(
                row.segment_code, "DOWNLOADING",
                "Exchange has not published the files yet — will retry next cycle",
                response=result.message,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        # failed / error / success-without-manifest: bounded retry budget.
        state = get_state(row, SegmentState.DOWNLOADING.value)
        attempts = int(state.get("failed_attempts", 0)) + 1
        state["failed_attempts"] = attempts
        set_state(row, SegmentState.DOWNLOADING.value, state)

        max_attempts = load_edp_config().edpb_download_max_attempts
        if result.is_transient or attempts < max_attempts:
            logger.warning(stage_log(
                row.segment_code, "DOWNLOADING",
                f"Download attempt {attempts}/{max_attempts} failed — will retry next cycle",
                error=result.message,
            ))
            if attempts < max_attempts:
                return SegmentHandlerResult(outcome=BLOCKED)
        logger.error(stage_log(
            row.segment_code, "DOWNLOADING",
            f"Download failed after {attempts} attempt(s) — marking FAILED",
            error=result.message,
        ))
        return self._fail_result(
            row, "DOWNLOAD_ERROR", f"edpb download failed after {attempts} attempt(s): {result.message}", now,
        )

    async def handle_uploading(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST /batches with the manifest DOWNLOADING recorded. Idempotent on
        the uploader side (batch_id), so re-entry after a crash simply gets
        200 already-known. 4xx (schema/checksum) is terminal — the manifest
        itself is bad; transport errors retry next cycle."""
        download = get_download_result(row)
        manifest_path = download.get("manifest_path")
        if not manifest_path:
            logger.error(stage_log(
                row.segment_code, "UPLOADING",
                "No manifest recorded from DOWNLOADING — cannot submit; marking FAILED",
            ))
            return self._fail_result(row, "UPLOAD_ERROR", "no manifest_path recorded by DOWNLOADING", now)

        client = get_edpb_client()
        result = await client.submit_batch(
            manifest_path, correlation_id=self._run_correlation_id(row),
        )
        record_poll(row, SegmentState.UPLOADING.value, "submit_batch",
                    f"accepted={result.accepted} {result.batch_status or result.message}"[:200], now)
        await session.flush()

        if result.accepted:
            logger.info(stage_log(
                row.segment_code, "UPLOADING",
                "Batch accepted by the uploader — proceeding to WAITING_FOR_FILE_UPLOAD",
                batch_id=result.batch_id, status=result.batch_status,
            ))
            mark_step_done(row, SegmentState.UPLOADING.value, "submit_batch",
                           result.batch_status or "accepted", now)
            return SegmentHandlerResult(
                outcome=ADVANCE, next_state=SegmentState.WAITING_FOR_FILE_UPLOAD, next_process=None,
            )

        if result.is_transient:
            logger.warning(stage_log(
                row.segment_code, "UPLOADING",
                "Uploader unreachable — will retry next cycle", error=result.message,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        logger.error(stage_log(
            row.segment_code, "UPLOADING",
            "Uploader rejected the batch — marking FAILED", error=result.message,
        ))
        return self._fail_result(row, "UPLOAD_ERROR", f"POST /batches rejected: {result.message}", now)

    # ---------------------------------------------------------------
    # WAITING_FOR_FILE_UPLOAD — operation on entry: READ the PID the
    # uploader reserved (retrying until it appears); then poll
    # FILEUPLOAD on every later entry.
    # ---------------------------------------------------------------

    async def handle_waiting_for_file_upload(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """
        First entries (row.process_id not yet resolved): read back the
        process ID the uploader reserved — one action, stays in this same
        state (BLOCKED — no transition) whether it resolved or the uploader
        hasn't reserved yet. Every later entry: poll
        file_process_status(FILEUPLOAD) until exchange files are uploaded,
        then advance to TRIGGERED.
        """
        if not row.process_id:
            return await self._resolve_process_id(cbos, row, login_id, now)
        return await self._poll_file_upload(cbos, row, session, login_id, now)

    async def _resolve_process_id(
        self, cbos: CbosClient, row: SegmentExecution, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """
        POST getdropdown(EXISTINGPROCESSID) — read the PID the uploader
        reserved for this segment/date. READ-ONLY by contract: the uploader
        is the sole reserver (single-writer, see module docstring), so a
        miss just means "uploader hasn't reserved yet" and we wait — this
        handler must never mint a PID of its own.
        """
        logger.info(stage_log(
            row.segment_code, "WAITING_FOR_FILE_UPLOAD",
            "Reading the uploader-reserved process ID (getdropdown EXISTINGPROCESSID)",
            trade_date=str(row.trade_date),
        ))

        existing = await cbos.get_existing_process_id(
            segment=row.segment_code, login_id=login_id, trade_date=row.trade_date,
        )

        if existing.found and existing.process_id:
            logger.info(stage_log(
                row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                "Process ID found — uploader has reserved it",
                pid=existing.process_id, desc=existing.description,
            ))
            return self._pid_resolved(row, existing.process_id, "EXISTING", now)

        if existing.error:
            if existing.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                    "Transient CBOS error on getdropdown(EXISTINGPROCESSID) — will retry next cycle",
                    error=existing.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                "Permanent CBOS error on getdropdown(EXISTINGPROCESSID) — marking FAILED",
                error=existing.error,
            ))
            return self._fail_result(
                row, "CBOS_ERROR", f"getdropdown(EXISTINGPROCESSID) failed: {existing.error}", now,
            )

        logger.info(stage_log(
            row.segment_code, "WAITING_FOR_FILE_UPLOAD",
            "No process ID yet — uploader hasn't reserved; waiting for the next cycle",
            trade_date=str(row.trade_date),
        ))
        return SegmentHandlerResult(outcome=BLOCKED)

    def _pid_resolved(
        self, row: SegmentExecution, process_id: str, source: str, now: datetime,
    ) -> SegmentHandlerResult:
        """Shared bookkeeping once the process_id resolves.
        Stays in WAITING_FOR_FILE_UPLOAD (BLOCKED — no state change); the
        next entry to this handler will see row.process_id set and poll
        FILEUPLOAD instead of looking the PID up again."""
        row.process_id = process_id
        row.process_id_reserved_at = now
        record_pid_reservation(row, process_id, source, now)

        logger.info(stage_log(
            row.segment_code, "WAITING_FOR_FILE_UPLOAD",
            f"Process ID resolved ({source}) — will poll FILEUPLOAD next cycle",
            pid=process_id, source=source, resolved_at=now.strftime("%H:%M:%S %Z"),
        ))
        return SegmentHandlerResult(outcome=BLOCKED, next_process="FILEUPLOAD")

    async def _poll_file_upload(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(FILEUPLOAD) — poll until exchange files are
        uploaded. For engine-driven batches, first consult the uploader's own
        batch status: a batch parked INCOMPLETE (completeness gate) means
        FILEUPLOAD will stay FALSE forever — fail loudly NOW (the terminal
        email alert is exactly how ops learns, per the gate's alerting
        decision) instead of a silent window-expiry hours later."""
        batch_id = get_download_result(row).get("batch_id")
        if batch_id:
            batch = await get_edpb_client().get_batch_status(
                batch_id, correlation_id=self._run_correlation_id(row),
            )
            if batch.found and batch.status == "incomplete":
                missing = ", ".join(
                    f"{slot.get('upload_id')} ({slot.get('name')})" for slot in batch.missing_slots
                ) or "unknown"
                logger.error(stage_log(
                    row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                    "Uploader parked the batch INCOMPLETE — mandatory files missing; "
                    "FILEUPLOAD cannot go TRUE. Failing now so ops is alerted "
                    "(fix: re-download, or audited POST /batches/{id}/proceed).",
                    batch_id=batch_id, missing_slots=missing,
                ))
                return self._fail_result(
                    row, "BATCH_INCOMPLETE",
                    f"uploader batch {batch_id} INCOMPLETE — missing mandatory slots: {missing}", now,
                )
            if batch.found and batch.status in ("failed", "rejected"):
                logger.error(stage_log(
                    row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                    f"Uploader reports batch {batch.status} — failing",
                    batch_id=batch_id,
                ))
                return self._fail_result(
                    row, "BATCH_FAILED", f"uploader batch {batch_id} is {batch.status}", now,
                )
            # unreachable / queued / uploading / confirmed / unconfirmed:
            # fall through to CBOS's own FILEUPLOAD verdict — the authority.

        result = await cbos.file_process_status(
            segment=row.segment_code, process_name="FILEUPLOAD", user_id=login_id,
        )
        record_poll(row, SegmentState.WAITING_FOR_FILE_UPLOAD.value, "FILEUPLOAD_STATUS", result.response, now)
        await session.flush()

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                    "Transient CBOS error — will retry next cycle", error=result.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"FILEUPLOAD check error: {result.error}", now)

        if result.is_pending:
            logger.info(stage_log(
                row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                "Exchange files not yet uploaded — waiting", response=result.response,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        # result.is_skip is deliberately not a distinct edge here — the
        # happy-flow tables document SKIPPED only off INIT (holiday check);
        # an unexpected SKIP mid-pipeline is treated as a CBOS error.
        if result.is_skip:
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_FILE_UPLOAD",
                "Unexpected SKIP for FILEUPLOAD (not a holiday-check state) — marking FAILED",
                response=result.response,
            ))
            return self._fail_result(row, "CBOS_ERROR", "Unexpected FILEUPLOAD SKIP response", now)

        logger.info(stage_log(
            row.segment_code, "WAITING_FOR_FILE_UPLOAD",
            "All exchange files uploaded — proceeding to TRIGGERED",
            response=result.response, ready_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_step_done(row, SegmentState.WAITING_FOR_FILE_UPLOAD.value, "FILEUPLOAD_STATUS", result.response, now)
        return SegmentHandlerResult(outcome=ADVANCE, next_state=SegmentState.TRIGGERED, next_process=None)

    # ---------------------------------------------------------------
    # TRIGGERED — the one genuine crash-safety-critical wait: fire
    # getNewTradeProcess with the real PID.
    # ---------------------------------------------------------------

    async def handle_triggered(
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
                row.segment_code, "TRIGGERED",
                "process_id missing — attempting crash recovery from CBOS",
                trade_date=str(row.trade_date),
            ))
            recovery = await cbos.get_existing_process_id(
                segment=row.segment_code, login_id=login_id, trade_date=row.trade_date,
            )
            if recovery.found and recovery.process_id:
                row.process_id = recovery.process_id
                logger.info(stage_log(
                    row.segment_code, "TRIGGERED", "process_id recovered from CBOS",
                    pid=recovery.process_id, desc=recovery.description,
                ))
            else:
                logger.error(stage_log(
                    row.segment_code, "TRIGGERED",
                    "Cannot recover process_id — marking FAILED", error=recovery.error,
                ))
                return self._fail_result(row, "CBOS_ERROR", "No process_id available for trigger", now)

        if get_state(row, SegmentState.TRIGGERED.value).get("status") == "TRIGGERING":
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
            row.segment_code, "TRIGGERED", "Firing process trigger (getNewTradeProcess)",
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
            row.segment_code, "TRIGGERED",
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
                    row.segment_code, "TRIGGERED",
                    "Transient CBOS error while checking recovery state — will retry next cycle",
                    pid=row.process_id, error=check.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "TRIGGERED",
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
                row.segment_code, "TRIGGERED",
                "CBOS already received/executing the trigger — NOT re-triggering; "
                "catching DB up to WAITING_FOR_BILLPOSTING",
                pid=row.process_id, steps=[f"{s.name}:{s.status}" for s in check.steps],
            ))
            return self._finalize_trigger_success(row, row.process_id, check.is_runnable, now)

        logger.info(stage_log(
            row.segment_code, "TRIGGERED",
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
                    row.segment_code, "TRIGGERED",
                    "Transient CBOS error — leaving TRIGGERING; will re-check next cycle",
                    pid=row.process_id, error=result.error,
                ))
                # Deliberately do NOT write processes_json here — it must
                # stay "TRIGGERING" so the next cycle goes through
                # _recover_trigger() instead of blindly re-firing.
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "TRIGGERED", "Trigger FAILED — marking segment FAILED",
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
            row.segment_code, "TRIGGERED", "Process TRIGGERED successfully — will poll BILLPOSTING next cycle",
            pid=process_id, is_runnable=is_runnable, triggered_at=now.strftime("%H:%M:%S %Z"),
        ))
        return SegmentHandlerResult(
            outcome=ADVANCE, next_state=SegmentState.WAITING_FOR_BILLPOSTING, next_process="BILLPOSTING",
        )

    # ---------------------------------------------------------------
    # WAITING_FOR_BILLPOSTING / WAITING_FOR_RECON /
    # WAITING_FOR_CONTRACT_NOTE_GENERATION — pure polls, CBOS auto-runs
    # each step once TRIGGERED fires; the agent only observes.
    # ---------------------------------------------------------------

    async def handle_waiting_for_billposting(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(BILLPOSTING) — wait until billing calculations complete."""
        return await self._poll_confirmation(
            cbos, row, session, login_id, now,
            process_name="BILLPOSTING", stage_key=SegmentState.WAITING_FOR_BILLPOSTING.value,
            next_state=SegmentState.WAITING_FOR_RECON, next_process="RECON",
        )

    async def handle_waiting_for_recon(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(RECON) — wait until reconciliation completes."""
        return await self._poll_confirmation(
            cbos, row, session, login_id, now,
            process_name="RECON", stage_key=SegmentState.WAITING_FOR_RECON.value,
            next_state=SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION, next_process="CONTRACTNOTEGENERATION",
        )

    async def handle_waiting_for_contract_note_generation(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(CONTRACTNOTEGENERATION) — wait until contract notes complete."""
        result = await cbos.file_process_status(
            segment=row.segment_code, process_name="CONTRACTNOTEGENERATION", user_id=login_id,
        )
        record_poll(
            row, SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION.value,
            "CONTRACTNOTEGENERATION_STATUS", result.response, now,
        )
        await session.flush()

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "WAITING_FOR_CONTRACT_NOTE_GENERATION",
                    "Transient CBOS error — will retry next cycle", error=result.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_CONTRACT_NOTE_GENERATION",
                "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"CONTRACTNOTEGENERATION error: {result.error}", now)

        if result.is_pending:
            logger.info(stage_log(
                row.segment_code, "WAITING_FOR_CONTRACT_NOTE_GENERATION",
                "Contract notes not yet generated — waiting", response=result.response,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        if result.is_skip:
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_CONTRACT_NOTE_GENERATION",
                "Unexpected SKIP for CONTRACTNOTEGENERATION — marking FAILED", response=result.response,
            ))
            return self._fail_result(row, "CBOS_ERROR", "Unexpected CONTRACTNOTEGENERATION SKIP response", now)

        logger.info(stage_log(
            row.segment_code, "WAITING_FOR_CONTRACT_NOTE_GENERATION",
            "Contract notes CONFIRMED — segment COMPLETED",
            response=result.response, confirmed_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_step_done(
            row, SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION.value,
            "CONTRACTNOTEGENERATION_STATUS", result.response, now,
        )
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
        next_state: SegmentState,
        next_process: str,
    ) -> SegmentHandlerResult:
        step_key = f"{process_name}_STATUS"
        result = await cbos.file_process_status(
            segment=row.segment_code, process_name=process_name, user_id=login_id,
        )
        record_poll(row, stage_key, step_key, result.response, now)
        await session.flush()

        state_name = f"WAITING_FOR_{process_name}"

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, state_name,
                    "Transient CBOS error — will retry next cycle", error=result.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, state_name, "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"{process_name} check error: {result.error}", now)

        if result.is_pending:
            logger.info(stage_log(
                row.segment_code, state_name,
                f"{process_name} not yet complete — waiting", response=result.response,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        if result.is_skip:
            logger.error(stage_log(
                row.segment_code, state_name,
                f"Unexpected SKIP for {process_name} — marking FAILED", response=result.response,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"Unexpected {process_name} SKIP response", now)

        logger.info(stage_log(
            row.segment_code, state_name, f"{process_name} CONFIRMED — advancing to {next_state.value}",
            response=result.response, confirmed_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_step_done(row, stage_key, step_key, result.response, now)
        return SegmentHandlerResult(outcome=ADVANCE, next_state=next_state, next_process=next_process)
