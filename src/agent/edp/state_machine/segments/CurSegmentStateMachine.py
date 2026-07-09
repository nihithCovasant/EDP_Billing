"""CD segment (CUR) — see CashSegmentStateMachine.py for the pattern; all
step logic lives in RealSegmentStateMachine."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class CurSegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "CUR"
