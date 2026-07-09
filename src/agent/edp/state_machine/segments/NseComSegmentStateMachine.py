"""NSE Commodity segment (NSECOM) — see CashSegmentStateMachine.py for the
pattern; all step logic lives in RealSegmentStateMachine."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class NseComSegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "NSECOM"
