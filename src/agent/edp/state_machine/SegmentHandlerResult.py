"""
Value object returned by every state handler (see AbstractStateMachine.py /
RealSegmentStateMachine.py / PostTradeStateMachine.py).

Replaces the old pipeline.stages.StageResult enum + direct row mutation:
handlers now report what happened and what should change, and
AbstractSegmentStateMachine.update_state() is the only place that actually
writes it to the row/DB.

Only one outcome moves a row forward a step: ADVANCE. There is no
loop-breaking outcome distinct from it — execute_handler() invokes exactly
one handler and returns, so there is nothing for a "stop, don't chain
further this cycle" outcome to distinguish itself from.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import SegmentState, SegmentStatus

# outcome values returned up through execute_handler() to the orchestrator.
ADVANCE = "advanced"
BLOCKED = "blocked"
COMPLETED = "completed"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class SegmentHandlerResult:
    outcome: str
    next_state: SegmentState | None = None
    next_process: str | None = None
    # Set only when outcome is completed/skipped/failed.
    next_status: SegmentStatus | None = None
    category: str | None = None
    reason: str | None = None
