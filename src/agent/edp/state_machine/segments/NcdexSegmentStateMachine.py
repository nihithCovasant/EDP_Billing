"""NCDEX segment — see CashSegmentStateMachine.py for the pattern; all step
logic lives in RealSegmentStateMachine."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class NcdexSegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "NCDEX"
