"""
Value object returned by every state handler (see AbstractStateMachine.py /
RealSegmentStateMachine.py / PostTradeStateMachine.py).

Replaces the old pipeline.stages.StageResult enum + direct row mutation:
handlers now report what happened and what should change, and
AbstractSegmentStateMachine.update_state() is the only place that actually
writes it to the row/DB.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import SegmentPhase, SegmentStatus

# outcome values returned up through execute_handler() to the orchestrator.
ADVANCE = "advance"
BLOCKED = "blocked"
STOP_NEXT = "stop_next"
COMPLETED = "completed"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class SegmentHandlerResult:
    outcome: str
    next_phase: SegmentPhase | None = None
    next_process: str | None = None
    # Set only when outcome is completed/skipped/failed.
    next_status: SegmentStatus | None = None
    category: str | None = None
    reason: str | None = None
