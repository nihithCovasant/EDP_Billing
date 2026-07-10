"""
Shared base for all 5 post-trade-process state machines (COLVAL, COLALLOC,
MTFFT, DMRPT, DMSTMT) — run once per trade_date, sequentially, after (but
independent of) the 10 real segments.

Happy-flow states (no "phases" — see models.SegmentState):
  WAITING_FOR_GTG -> [TRIGGERED ->] WAITING_FOR_COMPLETION -> (SUCCEEDED)

  WAITING_FOR_GTG    -> POST file_process_status(<process-specific
                         ProcessName>) — poll until ready; once ready, call
                         the process's "already triggered" CBOS check: if
                         already triggered, take the direct edge straight to
                         WAITING_FOR_COMPLETION (no new trigger fired);
                         otherwise move to TRIGGERED.
  TRIGGERED           -> POST <process-specific trigger endpoint> — the one
                         genuinely real crash-safety-critical wait in this
                         pipeline.
  WAITING_FOR_COMPLETION -> POST file_process_status(<same ProcessName>) —
                         poll again until CBOS confirms completion.

The 5 processes differ only in which CBOS endpoints TRIGGER_METHOD_NAME /
CHECK_TRIGGERED_METHOD_NAME dispatch to — each of the 5 files under
post_trade/ names both on CbosClient; the GTG/confirm poll and state
machine are otherwise identical for all 5. The GTG/confirm ProcessName is
ops-configurable (workflow_json["post_trade_processes"][].gtg_process_name),
resolved once when the process starts by orchestrator.py and read from
row.current_process thereafter.

Each handler call does exactly one action and returns — AbstractStateMachine
applies the resulting single state transition; there is no internal loop.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SegmentExecution, SegmentState
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
from .SegmentHandlerResult import ADVANCE, BLOCKED, SegmentHandlerResult
from .TradeSegmentTransitionFactory import POST_TRADE_TRANSITION_MAP
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger


class PostTradeStateMachine(AbstractSegmentStateMachine):
    # Overridden by each concrete leaf class (e.g. ColValStateMachine):
    #   TRIGGER_METHOD_NAME = "trigger_collateral_valuation" — CbosClient
    #     method handle_triggered() dispatches to.
    #   CHECK_TRIGGERED_METHOD_NAME = "check_collateral_valuation_triggered"
    #     — CbosClient method handle_waiting_for_gtg() dispatches to.
    TRIGGER_METHOD_NAME: str = ""
    CHECK_TRIGGERED_METHOD_NAME: str = ""

    def __init__(self) -> None:
        super().__init__(POST_TRADE_TRANSITION_MAP)

    def get_state_handler(self, state: SegmentState | None):
        handlers = {
            SegmentState.WAITING_FOR_GTG: self.handle_waiting_for_gtg,
            SegmentState.TRIGGERED: self.handle_triggered,
            SegmentState.WAITING_FOR_COMPLETION: self.handle_waiting_for_completion,
        }
        return handlers.get(state)

    # ---------------------------------------------------------------
    # WAITING_FOR_GTG — poll readiness, then decide direct-vs-triggered
    # ---------------------------------------------------------------

    async def handle_waiting_for_gtg(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """
        POST file_process_status(<ProcessName>) — poll until CBOS says
        ready. Once ready, call the "already triggered" check: if CBOS
        already has a run for this date, take the direct edge to
        WAITING_FOR_COMPLETION without firing a new trigger; otherwise
        move to TRIGGERED, which fires the trigger next cycle.
        """
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
                    row.segment_code, "WAITING_FOR_GTG",
                    "Transient CBOS error — will retry next cycle", error=result.error, poll=poll_count,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_GTG", "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"{process_name} GTG check error: {result.error}", now)

        if result.is_pending:
            if poll_count == 1 or poll_count % 5 == 0:
                logger.info(stage_log(
                    row.segment_code, "WAITING_FOR_GTG",
                    f"{process_name} not yet ready — waiting", response=result.response, poll=poll_count,
                ))
            return SegmentHandlerResult(outcome=BLOCKED)

        if result.is_skip:
            # The post-trade happy-flow table has no SKIPPED edge at all.
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_GTG",
                f"Unexpected SKIP for {process_name} — marking FAILED",
                response=result.response, poll=poll_count,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"Unexpected {process_name} SKIP response", now)

        mark_stage_done(row, "gtg", result.response, now)
        return await self._decide_direct_or_triggered(cbos, row, login_id, now, process_name)

    async def _decide_direct_or_triggered(
        self, cbos: CbosClient, row: SegmentExecution, login_id: str, now: datetime, process_name: str,
    ) -> SegmentHandlerResult:
        if not self.CHECK_TRIGGERED_METHOD_NAME:
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_GTG",
                "Unknown post-trade process code — marking FAILED",
            ))
            return self._fail_result(row, "CBOS_ERROR", f"Unknown post-trade process code {row.segment_code}", now)

        check_fn = getattr(cbos, self.CHECK_TRIGGERED_METHOD_NAME)
        check = await check_fn(login_id, row.trade_date)

        if check.error and check.is_transient:
            logger.warning(stage_log(
                row.segment_code, "WAITING_FOR_GTG",
                "Transient CBOS error on already-triggered check — will retry next cycle",
                error=check.error,
            ))
            return SegmentHandlerResult(outcome=BLOCKED)

        if check.already_triggered:
            logger.info(stage_log(
                row.segment_code, "WAITING_FOR_GTG",
                f"{process_name} already triggered — taking direct edge to "
                "WAITING_FOR_COMPLETION, no new trigger fired",
                ready_at=now.strftime("%H:%M:%S %Z"),
            ))
            return SegmentHandlerResult(
                outcome=ADVANCE, next_state=SegmentState.WAITING_FOR_COMPLETION, next_process=process_name,
            )

        logger.info(stage_log(
            row.segment_code, "WAITING_FOR_GTG",
            f"{process_name} GTG confirmed, not yet triggered — proceeding to TRIGGERED",
            ready_at=now.strftime("%H:%M:%S %Z"),
        ))
        return SegmentHandlerResult(
            outcome=ADVANCE, next_state=SegmentState.TRIGGERED, next_process=process_name,
        )

    # ---------------------------------------------------------------
    # TRIGGERED — the one genuinely real crash-safety-critical wait
    # ---------------------------------------------------------------

    async def handle_triggered(
        self, cbos: CbosClient, row: SegmentExecution, session: AsyncSession, login_id: str, now: datetime,
    ) -> SegmentHandlerResult:
        """
        POST the process-specific trigger endpoint named by TRIGGER_METHOD_NAME.

        Crash safety: the "already triggered" check at WAITING_FOR_GTG
        already ruled out a prior CBOS-confirmed run before this state was
        entered, but a crash could still land between committing our own
        "TRIGGERING" marker and confirming the outcome. In that case we
        refuse to blindly re-fire and mark FAILED for manual verification
        instead — an operator verifies with CBOS before retrying.
        """
        code = row.segment_code
        if not self.TRIGGER_METHOD_NAME:
            logger.error(stage_log(code, "TRIGGERED", "Unknown post-trade process code — marking FAILED"))
            return self._fail_result(row, "CBOS_ERROR", f"Unknown post-trade process code {code}", now)

        if get_proc(row, "trigger").get("status") == "TRIGGERING":
            logger.error(stage_log(
                code, "TRIGGERED",
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
        logger.info(stage_log(code, "TRIGGERED", "Firing post-trade trigger", triggered_at=now.strftime("%H:%M:%S %Z")))

        # All 5 trigger methods share this signature; CbosClient handles
        # the per-endpoint JSON key differences (MARGINDATE vs TRADEDATE) internally.
        result = await trigger_fn(login_id, row.trade_date)

        if not result.success:
            record_post_trade_trigger_failed(row, result.error or "TRIGGER_FAILED", now)
            if result.is_transient:
                logger.warning(stage_log(
                    code, "TRIGGERED", "Transient CBOS error — will retry trigger next cycle", error=result.error,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(code, "TRIGGERED", "Trigger FAILED — marking process FAILED", error=result.error))
            return self._fail_result(row, "CBOS_ERROR", f"{self.TRIGGER_METHOD_NAME} failed: {result.error}", now)

        record_post_trade_trigger(row, result.message, now)

        logger.info(stage_log(
            code, "TRIGGERED", "Trigger acknowledged — will poll for confirmation next cycle",
            cbos_message=result.message, triggered_at=now.strftime("%H:%M:%S %Z"),
        ))
        # current_process already holds the resolved ProcessName from WAITING_FOR_GTG — leave it unchanged.
        return SegmentHandlerResult(
            outcome=ADVANCE, next_state=SegmentState.WAITING_FOR_COMPLETION, next_process=row.current_process,
        )

    # ---------------------------------------------------------------
    # WAITING_FOR_COMPLETION — pure poll
    # ---------------------------------------------------------------

    async def handle_waiting_for_completion(
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
                    row.segment_code, "WAITING_FOR_COMPLETION",
                    "Transient CBOS error — will retry next cycle", error=result.error, poll=poll_count,
                ))
                return SegmentHandlerResult(outcome=BLOCKED)
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_COMPLETION", "Permanent CBOS error — marking FAILED", error=result.error,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"{process_name} confirm check error: {result.error}", now)

        if result.is_pending:
            if poll_count == 1 or poll_count % 5 == 0:
                logger.info(stage_log(
                    row.segment_code, "WAITING_FOR_COMPLETION",
                    f"{process_name} not yet complete — waiting", response=result.response, poll=poll_count,
                ))
            return SegmentHandlerResult(outcome=BLOCKED)

        if result.is_skip:
            logger.error(stage_log(
                row.segment_code, "WAITING_FOR_COMPLETION",
                f"Unexpected SKIP for {process_name} — marking FAILED",
                response=result.response, poll=poll_count,
            ))
            return self._fail_result(row, "CBOS_ERROR", f"Unexpected {process_name} SKIP response", now)

        logger.info(stage_log(
            row.segment_code, "WAITING_FOR_COMPLETION", f"{process_name} CONFIRMED — post-trade process COMPLETED",
            response=result.response, total_polls=poll_count, confirmed_at=now.strftime("%H:%M:%S %Z"),
        ))
        mark_stage_done(row, "confirm", result.response, now)
        return self._complete_result(row, now)
