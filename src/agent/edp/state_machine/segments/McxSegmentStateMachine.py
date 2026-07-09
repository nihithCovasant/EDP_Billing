"""MCX segment — see CashSegmentStateMachine.py for the pattern; all step
logic lives in RealSegmentStateMachine."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class McxSegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "MCX"
