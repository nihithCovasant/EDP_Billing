"""Mutual Fund segment (MF) — see CashSegmentStateMachine.py for the
pattern; all step logic lives in RealSegmentStateMachine."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class MfSegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "MF"
