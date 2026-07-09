"""
Shared base for all 5 post-trade-process state machines (COLVAL, COLALLOC,
MTFFT, DMRPT, DMSTMT) — run once per trade_date, sequentially, after (but
independent of) the 10 real segments.

  AWAIT_GTG     -> POST file_process_status(<process-specific ProcessName>) — poll
  TRIGGER_JOB   -> POST <process-specific trigger endpoint>
  AWAIT_CONFIRM -> POST file_process_status(<same ProcessName>) — poll again

The 5 processes differ only in which CBOS trigger endpoint
handle_trigger_job() dispatches to — each of the 5 files under post_trade/
sets TRIGGER_METHOD_NAME to name that endpoint on CbosClient; the GTG/confirm
poll and state machine are otherwise identical for all 5. The GTG/confirm
ProcessName is ops-configurable (workflow_json["post_trade_processes"][].
gtg_process_name), resolved once when the process starts by orchestrator.py
and read from row.current_process thereafter.

Ported from the old pipeline/post_trade_stages.py free functions, which
themselves reused pipeline/stages.py's _fail/_skip/_complete — same sharing
here, via AbstractStateMachine's _fail_result/_skip_result/_complete_result.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SegmentExecution, SegmentPhase
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
from .AbstractStateMachine import AbstractSegmentStateMachine
from .SegmentHandlerResult import ADVANCE, BLOCKED, STOP_NEXT, SegmentHandlerResult
from .SegmentTransitionMap import POST_TRADE_TRANSITION_MAP
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


class PostTradeStateMachine(AbstractSegmentStateMachine):
    # Overridden by each concrete leaf class (e.g. ColValStateMachine.TRIGGER_METHOD_NAME
    # = "trigger_collateral_valuation") — the CbosClient method handle_trigger_job() dispatches to.
    TRIGGER_METHOD_NAME: str = ""

    def __init__(self) -> None:
        super().__init__(POST_TRADE_TRANSITION_MAP)

    def get_state_handler(self, phase: SegmentPhase | None):
        handlers = {
            SegmentPhase.AWAIT_GTG: self.handle_await_gtg,
            SegmentPhase.TRIGGER_JOB: self.handle_trigger_job,
            SegmentPhase.AWAIT_CONFIRM: self.handle_await_confirm,
        }
        return handlers.get(phase)

    # ---------------------------------------------------------------
    # Stage 1 — Await GTG (Good To Go)
    # ---------------------------------------------------------------

    async def handle_await_gtg(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(<ProcessName>) — poll until CBOS says ready to trigger."""
        # Resolved and persisted at process start (see
        # orchestrator._resolve_post_trade_process_name()); survives a restart mid-poll.
        process_name = row.current_process or POST_TRADE_GTG_PROCESS_NAME.get(row.segment_code, row.segment_code)
        poll_state = get_proc(row, "gtg")
        poll_count = poll_state.get("poll_count", 0) + 1

        result = await cbos.file_process_status(
            segment=row.segment_code, process_name=process_name, user_id=login_id,
        )
        inc_poll(row, "gtg", result.response)
        await session.flush()

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "AWAIT_GTG",
                    "Transient CBOS error — will retry next cycle", error=result.error, poll=poll_count,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "AWAIT_GTG", "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"{process_name} GTG check error: {result.error}", now)

        if result.is_skip:
            logger.info(stage_log(
                row.segment_code, "AWAIT_GTG",
                f"CBOS returned SKIP for {process_name} — process will be SKIPPED",
                response=result.response, poll=poll_count,
            ))
            return self._skip_result(row, "CBOS_SKIP", f"{process_name} returned SKIP", now)

        if result.is_pending:
            if poll_count == 1 or poll_count % 5 == 0:
                logger.info(stage_log(
                    row.segment_code, "AWAIT_GTG",
                    f"{process_name} not yet ready — waiting", response=result.response, poll=poll_count,
                ))
            return SegmentHandlerResult(outcome=BLOCKED)

        logger.info(stage_log(
            row.segment_code, "AWAIT_GTG", f"{process_name} GTG confirmed — proceeding to TRIGGER_JOB",
            response=result.response, total_polls=poll_count, ready_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_stage_done(row, "gtg", result.response, now)
        return SegmentHandlerResult(outcome=ADVANCE, next_phase=SegmentPhase.TRIGGER_JOB, next_process=None)

    # ---------------------------------------------------------------
    # Stage 2 — Trigger the post-trade job
    # ---------------------------------------------------------------

    async def handle_trigger_job(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """
        POST the process-specific trigger endpoint named by TRIGGER_METHOD_NAME.

        Crash safety: unlike the real-segment TRIGGER step, there's no
        PROCESSID/Table2 equivalent to ask CBOS "did you get my last
        call?". So if our own "TRIGGERING" marker is already set, we
        refuse to re-fire and mark FAILED with a "needs manual CBOS
        verification" reason instead — an operator verifies with CBOS
        before retrying.
        """
        code = row.segment_code
        if not self.TRIGGER_METHOD_NAME:
            logger.error(stage_log(code, "TRIGGER_JOB", "Unknown post-trade process code — marking FAILED"))
            return self._fail_result(row, "CBOS_ERROR", f"Unknown post-trade process code {code}", now)

        if get_proc(row, "trigger").get("status") == "TRIGGERING":
            logger.error(stage_log(
                code, "TRIGGER_JOB",
                "Resuming with an unconfirmed prior trigger attempt — refusing to "
                "re-fire; marking FAILED for manual verification",
            ))
            return self._fail_result(
                row, "CBOS_ERROR",
                "Unconfirmed trigger attempt after restart — verify with CBOS directly before retrying",
                now,
            )

        # Pre-commit marker BEFORE the CBOS call, durably committed so a
        # crash in between can never silently revert to "never attempted".
        record_post_trade_trigger_attempt(row, now)
        await session.commit()

        trigger_fn = getattr(cbos, self.TRIGGER_METHOD_NAME)
        logger.info(stage_log(code, "TRIGGER_JOB", "Firing post-trade trigger", triggered_at=now.strftime("%H:%M:%S %Z")))

        # All 5 trigger methods share this signature; CbosClient handles
        # the per-endpoint JSON key differences (MARGINDATE vs TRADEDATE) internally.
        result = await trigger_fn(login_id, row.trade_date)

        if not result.success:
            record_post_trade_trigger_failed(row, result.error or "TRIGGER_FAILED", now)
            if result.is_transient:
                logger.warning(stage_log(
                    code, "TRIGGER_JOB", "Transient CBOS error — will retry trigger next cycle", error=result.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(code, "TRIGGER_JOB", "Trigger FAILED — marking process FAILED", error=result.error))
            return self._fail_result(row, "CBOS_ERROR", f"{self.TRIGGER_METHOD_NAME} failed: {result.error}", now)

        record_post_trade_trigger(row, result.message, now)

        logger.info(stage_log(
            code, "TRIGGER_JOB", "Trigger acknowledged — will poll for confirmation next cycle",
            cbos_message=result.message, triggered_at=now.strftime("%H:%M:%S %Z"),
        ))
        # current_process already holds the resolved ProcessName from AWAIT_GTG — leave it unchanged.
        return SegmentHandlerResult(
            outcome=STOP_NEXT, next_phase=SegmentPhase.AWAIT_CONFIRM, next_process=row.current_process,
        )

    # ---------------------------------------------------------------
    # Stage 3 — Await confirmation
    # ---------------------------------------------------------------

    async def handle_await_confirm(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """POST file_process_status(<ProcessName>) again — poll until CBOS confirms completion."""
        process_name = row.current_process or POST_TRADE_GTG_PROCESS_NAME.get(row.segment_code, row.segment_code)
        poll_state = get_proc(row, "confirm")
        poll_count = poll_state.get("poll_count", 0) + 1

        result = await cbos.file_process_status(
            segment=row.segment_code, process_name=process_name, user_id=login_id,
        )
        inc_poll(row, "confirm", result.response)
        await session.flush()

        if result.is_error:
            if result.is_transient:
                logger.warning(stage_log(
                    row.segment_code, "AWAIT_CONFIRM",
                    "Transient CBOS error — will retry next cycle", error=result.error, poll=poll_count,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "AWAIT_CONFIRM", "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"{process_name} confirm check error: {result.error}", now)

        if result.is_skip:
            logger.info(stage_log(
                row.segment_code, "AWAIT_CONFIRM",
                f"CBOS returned SKIP for {process_name} — process will be SKIPPED",
                response=result.response, poll=poll_count,
            ))
            return self._skip_result(row, "CBOS_SKIP", f"{process_name} returned SKIP", now)

        if result.is_pending:
            if poll_count == 1 or poll_count % 5 == 0:
                logger.info(stage_log(
                    row.segment_code, "AWAIT_CONFIRM",
                    f"{process_name} not yet complete — waiting", response=result.response, poll=poll_count,
                ))
            return SegmentHandlerResult(outcome=BLOCKED)

        logger.info(stage_log(
            row.segment_code, "AWAIT_CONFIRM", f"{process_name} CONFIRMED — post-trade process COMPLETED",
            response=result.response, total_polls=poll_count, confirmed_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_stage_done(row, "confirm", result.response, now)
        return self._complete_result(row, now)
