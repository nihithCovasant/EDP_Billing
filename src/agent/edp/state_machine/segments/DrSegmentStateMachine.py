"""F&O segment (DR) — see CashSegmentStateMachine.py for the pattern; all
step logic lives in RealSegmentStateMachine."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class DrSegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "DR"
