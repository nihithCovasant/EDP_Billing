"""
AbstractSegmentStateMachine — the ABC every concrete segment/post-trade
state machine subclasses. Mirrors finite_state_machine
1/finite_state_machine/AbstractStateMachine.py's shape (get_state_handler,
update_state, is_my_time_window, is_my_window_over, execute_handler), with
two deliberate differences from that sketch:

  - update_state() is concrete here (not abstract) — it's the direct
    analogue of repository.segment.move_to_state(), which it delegates to
    for terminal transitions so the existing email-alert logic is reused
    as-is, not reimplemented.
  - execute_handler() loops internally while a handler keeps returning
    ADVANCE, instead of doing one phase per call — this preserves the
    existing fast multi-phase-per-wake-cycle behavior (see
    pipeline/executor.py::advance_pipeline(), which this method replaces).

Terminal states need no handler: get_state_handler(SegmentPhase.DONE)
returns None, and callers (orchestrator.py) never invoke execute_handler()
on an already-terminal row (see repository.is_handled()) — so there are no
handle_completed/handle_failed/handle_skipped methods anywhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .. import repository
from ..models import SegmentExecution, SegmentPhase, SegmentStatus
from ..utils.datetime_utils import ensure_aware, now_ist
from ..utils.log_fmt import stage_log
from .SegmentHandlerResult import (
    ADVANCE,
    BLOCKED,
    COMPLETED,
    FAILED,
    SKIPPED,
    STOP_NEXT,
    SegmentHandlerResult,
)
from .SegmentTransitionMap import SegmentTransitionMap
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger

HandlerFn = Callable[
    [CbosClient, SegmentExecution, AsyncSession, str, datetime],
    Awaitable[SegmentHandlerResult],
]

_TERMINAL_OUTCOMES = (COMPLETED, SKIPPED, FAILED)


class AbstractSegmentStateMachine(ABC):
    # Overridden by each concrete leaf class (e.g. CashSegmentStateMachine.SEGMENT_CODE = "EQ").
    SEGMENT_CODE: str = ""

    def __init__(self, transition_map: SegmentTransitionMap):
        self.transition_map = transition_map

    @abstractmethod
    def get_state_handler(self, phase: SegmentPhase | None) -> Optional[HandlerFn]:
        """Resolve the handler bound to the current phase. Returns None for
        DONE or an unmapped/corrupt phase — never raises."""
        raise NotImplementedError

    # -------------------------------------------------------------------
    # Concrete — state transition + window helpers
    # -------------------------------------------------------------------

    async def update_state(
        self,
        session: AsyncSession,
        row: SegmentExecution,
        result: SegmentHandlerResult,
        now: datetime,
    ) -> None:
        """
        Apply a SegmentHandlerResult to `row`. A terminal result routes
        through repository.move_to_state() (bookkeeping + email alert);
        anything else is just a phase/process move, flushed here.
        """
        if result.next_status is not None:
            await repository.move_to_state(
                session, row, result.next_status,
                category=result.category, reason=result.reason, now=now,
            )
            return
        if result.next_phase is not None:
            row.current_phase = result.next_phase
        if result.next_process is not None or result.outcome in (ADVANCE, STOP_NEXT):
            row.current_process = result.next_process
        await session.flush()

    def get_current_state(self, row: SegmentExecution) -> SegmentPhase | None:
        return row.current_phase

    def is_my_time_window(self, now: datetime, window_start: datetime | None) -> bool:
        """True if `now` has reached the configured opening gate (or there
        is none). Called by the orchestrator before execute_handler() to
        decide whether to run this cycle at all; execute_handler() itself
        assumes the window is already open and does not re-check this."""
        return window_start is None or now >= window_start

    def is_my_window_over(self, now: datetime, window_end: datetime | None) -> bool:
        return window_end is not None and now > window_end

    def _resolve_target_state(self, phase: SegmentPhase | None, result: SegmentHandlerResult):
        if result.next_status is not None:
            return result.next_status
        if result.next_phase is not None:
            return result.next_phase
        return phase  # BLOCKED / no-op this cycle

    def _validate_transition(self, phase: SegmentPhase | None, result: SegmentHandlerResult) -> bool:
        target = self._resolve_target_state(phase, result)
        return self.transition_map.is_allowed(self.SEGMENT_CODE, phase, target)

    # -------------------------------------------------------------------
    # Concrete — shared terminal-result builders. Identical wording for
    # every segment/process family (real segments and post-trade
    # processes alike used to share these via pipeline/stages.py's
    # _fail/_skip/_complete — same sharing here, just as methods).
    # -------------------------------------------------------------------

    def _fail_result(
        self, row: SegmentExecution, category: str, reason: str, now: datetime,
    ) -> SegmentHandlerResult:
        logger.error(stage_log(
            row.segment_code,
            row.current_phase.value if row.current_phase else "UNKNOWN",
            "Stage FAILED — marking segment FAILED",
            category=category,
            reason=reason,
            failed_at=now.strftime("%H:%M:%S %Z"),
        ))
        return SegmentHandlerResult(
            outcome=FAILED, next_status=SegmentStatus.FAILED, category=category, reason=reason,
        )

    def _skip_result(
        self, row: SegmentExecution, category: str, reason: str, now: datetime,
    ) -> SegmentHandlerResult:
        logger.info(stage_log(
            row.segment_code,
            row.current_phase.value if row.current_phase else "UNKNOWN",
            "Segment SKIPPED",
            category=category,
            reason=reason,
            skipped_at=now.strftime("%H:%M:%S %Z"),
        ))
        return SegmentHandlerResult(
            outcome=SKIPPED, next_status=SegmentStatus.SKIPPED, category=category, reason=reason,
        )

    def _complete_result(self, row: SegmentExecution, now: datetime) -> SegmentHandlerResult:
        logger.info(stage_log(
            row.segment_code, "DONE", "Segment fully COMPLETED",
            completed_at=now.strftime("%H:%M:%S %Z"),
        ))
        return SegmentHandlerResult(outcome=COMPLETED, next_status=SegmentStatus.COMPLETED)

    # -------------------------------------------------------------------
    # Concrete — the engine loop (replaces pipeline.executor.advance_pipeline)
    # -------------------------------------------------------------------

    async def execute_handler(
        self,
        cbos: CbosClient,
        row: SegmentExecution,
        session: AsyncSession,
        login_id: str,
        now: datetime,
        window_end: datetime | None = None,
    ) -> str:
        """
        Drive `row` through its pipeline until it blocks, completes, or
        fails. Returns one of: "completed" | "skipped" | "failed" |
        "advanced" | "blocked". window_end is None for post-trade rows
        (no deadline).
        """
        window_end = ensure_aware(window_end)
        while True:
            now = now_ist()

            phase = self.get_current_state(row)

            if self.is_my_window_over(now, window_end) and phase not in (SegmentPhase.DONE, None):
                timed_out_phase = phase.value
                logger.warning(stage_log(
                    row.segment_code,
                    timed_out_phase,
                    "Window deadline exceeded while IN_PROGRESS — marking FAILED",
                    deadline=window_end.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                    phase=timed_out_phase,
                ))
                await self.update_state(session, row, SegmentHandlerResult(
                    outcome=FAILED,
                    next_status=SegmentStatus.FAILED,
                    category="TIMEOUT",
                    reason=f"Exceeded window deadline {window_end.isoformat()} at phase {timed_out_phase}",
                ), now)
                return FAILED

            handler = self.get_state_handler(phase)

            if handler is None:
                if phase == SegmentPhase.DONE:
                    return COMPLETED
                logger.error(stage_log(
                    row.segment_code, str(phase),
                    "No handler registered for this phase — marking FAILED",
                ))
                await self.update_state(session, row, SegmentHandlerResult(
                    outcome=FAILED,
                    next_status=SegmentStatus.FAILED,
                    category="SYSTEM_ERROR",
                    reason=f"No pipeline handler registered for phase={phase}",
                ), now)
                return FAILED

            result: SegmentHandlerResult = await handler(cbos, row, session, login_id, now)

            if not self._validate_transition(phase, result):
                target = self._resolve_target_state(phase, result)
                logger.error(stage_log(
                    row.segment_code, phase.value if phase else str(phase),
                    "Invalid state transition attempted — marking FAILED",
                    attempted_target=str(target),
                    allowed=str(self.transition_map.get_segment_transitions(self.SEGMENT_CODE).get(phase)),
                ))
                await self.update_state(session, row, SegmentHandlerResult(
                    outcome=FAILED,
                    next_status=SegmentStatus.FAILED,
                    category="SYSTEM_ERROR",
                    reason=f"Invalid transition {phase} -> {target}",
                ), now)
                return FAILED

            if result.outcome in _TERMINAL_OUTCOMES:
                await self.update_state(session, row, result, now)
                return result.outcome

            if result.outcome == BLOCKED:
                return BLOCKED

            if result.outcome == STOP_NEXT:
                await self.update_state(session, row, result, now)
                return "advanced"

            # ADVANCE — apply the phase/process move, log, and loop again
            # immediately so a chain of instantly-ready CBOS responses
            # still finishes within one wake cycle.
            await self.update_state(session, row, result, now)
            next_phase = row.current_phase
            logger.info(stage_log(
                row.segment_code,
                phase.value if phase else str(phase),
                f"Phase complete — advancing to {next_phase.value if next_phase else next_phase}",
            ))
