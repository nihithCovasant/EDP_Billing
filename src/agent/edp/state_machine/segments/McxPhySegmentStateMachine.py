"""MCX Phy segment (physical-settlement counterpart of MCX, run immediately
after it) — see CashSegmentStateMachine.py for the pattern; all step logic
lives in RealSegmentStateMachine."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class McxPhySegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "MCXPHY"
