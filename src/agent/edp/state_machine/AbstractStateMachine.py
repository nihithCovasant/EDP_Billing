"""
AbstractSegmentStateMachine — the ABC every concrete segment/post-trade
state machine subclasses. Mirrors finite_state_machine
1/finite_state_machine/AbstractStateMachine.py's shape (get_state_handler,
update_state, is_my_time_window, is_my_window_over, execute_handler).

There are no "phases" — only states. And there is exactly ONE loop in the
whole system: loop.py's wake cycle. execute_handler() invokes exactly one
state's handler and returns immediately — it never chains multiple states
within a single call. A full pipeline run therefore spans multiple wake
cycles (one state transition per cycle per segment/process), not one.

  - update_state() is concrete here (not abstract) — it's the direct
    analogue of repository.segment.move_to_state(), which it delegates to
    for terminal transitions so the existing email-alert logic is reused
    as-is, not reimplemented.

Terminal states need no handler: get_state_handler(None) after a terminal
segment_status returns None, and callers (orchestrator.py) never invoke
execute_handler() on an already-terminal row (see repository.is_handled())
— so there are no handle_completed/handle_failed/handle_skipped methods
anywhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime

from cams_otel_lib import Logger as logger
from sqlalchemy.ext.asyncio import AsyncSession

from src.tools.cbos_client import CbosClient

from .. import repository
from ..models import SegmentExecution, SegmentState, SegmentStatus
from ..utils.datetime_utils import ensure_aware
from ..utils.log_fmt import stage_log
from .SegmentHandlerResult import (
    ADVANCE,
    BLOCKED,
    COMPLETED,
    FAILED,
    SKIPPED,
    SegmentHandlerResult,
)
from .SegmentTransitionMap import SegmentTransitionMap

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
    def get_state_handler(self, state: SegmentState | None) -> HandlerFn | None:
        """Resolve the handler bound to the current state. Returns None for
        an already-terminal or an unmapped/corrupt state — never raises."""
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
        anything else is just a state/process move, flushed here.
        """
        if result.next_status is not None:
            await repository.move_to_state(
                session,
                row,
                result.next_status,
                category=result.category,
                reason=result.reason,
                now=now,
            )
            return
        if result.next_state is not None:
            row.current_state = result.next_state
        if result.next_process is not None or result.outcome == ADVANCE:
            row.current_process = result.next_process
        await session.flush()

    def get_current_state(self, row: SegmentExecution) -> SegmentState | None:
        return row.current_state

    def is_my_time_window(self, now: datetime, window_start: datetime | None) -> bool:
        """True if `now` has reached the configured opening gate (or there
        is none). Called by the orchestrator before execute_handler() to
        decide whether to run this cycle at all; execute_handler() itself
        assumes the window is already open and does not re-check this."""
        return window_start is None or now >= window_start

    def is_my_window_over(self, now: datetime, window_end: datetime | None) -> bool:
        return window_end is not None and now > window_end

    def _resolve_target_state(self, state: SegmentState | None, result: SegmentHandlerResult):
        if result.next_status is not None:
            return result.next_status
        if result.next_state is not None:
            return result.next_state
        return state  # BLOCKED / no-op this cycle

    def _validate_transition(self, state: SegmentState | None, result: SegmentHandlerResult) -> bool:
        target = self._resolve_target_state(state, result)
        return self.transition_map.is_allowed(self.SEGMENT_CODE, state, target)

    # -------------------------------------------------------------------
    # Concrete — shared terminal-result builders. Identical wording for
    # every segment/process family (real segments and post-trade
    # processes alike share these via inherited methods, not copy-pasted
    # per family).
    # -------------------------------------------------------------------

    def _fail_result(
        self,
        row: SegmentExecution,
        category: str,
        reason: str,
        now: datetime,
    ) -> SegmentHandlerResult:
        logger.error(
            stage_log(
                row.segment_code,
                row.current_state.value if row.current_state else "UNKNOWN",
                "State FAILED — marking segment FAILED",
                category=category,
                reason=reason,
                failed_at=now.strftime("%H:%M:%S %Z"),
            )
        )
        return SegmentHandlerResult(
            outcome=FAILED,
            next_status=SegmentStatus.FAILED,
            category=category,
            reason=reason,
        )

    def _skip_result(
        self,
        row: SegmentExecution,
        category: str,
        reason: str,
        now: datetime,
    ) -> SegmentHandlerResult:
        logger.info(
            stage_log(
                row.segment_code,
                row.current_state.value if row.current_state else "UNKNOWN",
                "Segment SKIPPED",
                category=category,
                reason=reason,
                skipped_at=now.strftime("%H:%M:%S %Z"),
            )
        )
        return SegmentHandlerResult(
            outcome=SKIPPED,
            next_status=SegmentStatus.SKIPPED,
            category=category,
            reason=reason,
        )

    def _complete_result(self, row: SegmentExecution, now: datetime) -> SegmentHandlerResult:
        logger.info(
            stage_log(
                row.segment_code,
                "SUCCEEDED",
                "Segment fully COMPLETED",
                completed_at=now.strftime("%H:%M:%S %Z"),
            )
        )
        return SegmentHandlerResult(outcome=COMPLETED, next_status=SegmentStatus.COMPLETED)

    # -------------------------------------------------------------------
    # Concrete — invoke exactly one handler and return. No loop here; the
    # only loop in the system is loop.py's wake cycle.
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
        Run the current state's handler ONCE and apply at most one state
        transition. Returns one of: "completed" | "skipped" | "failed" |
        "advanced" | "blocked". window_end is a real deadline for both real
        segments and post-trade processes (see orchestrator._resolve_window()
        / _resolve_post_trade_window_end()) — a stuck poll always eventually
        fails loudly instead of blocking forever with no alert. Multi-state
        progress happens across multiple calls (i.e. multiple wake cycles),
        never within a single call.
        """
        window_end = ensure_aware(window_end)
        now = ensure_aware(now)

        state = self.get_current_state(row)

        if self.is_my_window_over(now, window_end) and state is not None:
            timed_out_state = state.value
            logger.warning(
                stage_log(
                    row.segment_code,
                    timed_out_state,
                    "Window deadline exceeded while IN_PROGRESS — marking FAILED",
                    deadline=window_end.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                    state=timed_out_state,
                )
            )
            await self.update_state(
                session,
                row,
                SegmentHandlerResult(
                    outcome=FAILED,
                    next_status=SegmentStatus.FAILED,
                    category="TIMEOUT",
                    reason=f"Exceeded window deadline {window_end.isoformat()} at state {timed_out_state}",
                ),
                now,
            )
            return FAILED

        handler = self.get_state_handler(state)

        if handler is None:
            logger.error(
                stage_log(
                    row.segment_code,
                    str(state),
                    "No handler registered for this state — marking FAILED",
                )
            )
            await self.update_state(
                session,
                row,
                SegmentHandlerResult(
                    outcome=FAILED,
                    next_status=SegmentStatus.FAILED,
                    category="SYSTEM_ERROR",
                    reason=f"No handler registered for state={state}",
                ),
                now,
            )
            return FAILED

        result: SegmentHandlerResult = await handler(cbos, row, session, login_id, now)

        if not self._validate_transition(state, result):
            target = self._resolve_target_state(state, result)
            logger.error(
                stage_log(
                    row.segment_code,
                    state.value if state else str(state),
                    "Invalid state transition attempted — marking FAILED",
                    attempted_target=str(target),
                    allowed=str(self.transition_map.get_segment_transitions(self.SEGMENT_CODE).get(state)),
                )
            )
            await self.update_state(
                session,
                row,
                SegmentHandlerResult(
                    outcome=FAILED,
                    next_status=SegmentStatus.FAILED,
                    category="SYSTEM_ERROR",
                    reason=f"Invalid transition {state} -> {target}",
                ),
                now,
            )
            return FAILED

        await self.update_state(session, row, result, now)

        if result.outcome in _TERMINAL_OUTCOMES:
            return result.outcome

        if result.outcome == ADVANCE:
            next_state = row.current_state
            logger.info(
                stage_log(
                    row.segment_code,
                    state.value if state else str(state),
                    f"State complete — advancing to {next_state.value if next_state else next_state}",
                )
            )
            return ADVANCE

        # BLOCKED — no state change; will re-check the same state next cycle.
        return BLOCKED
