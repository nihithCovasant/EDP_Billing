"""NCDEX Phy segment (physical-settlement counterpart of NCDEX, run
immediately after it) — see CashSegmentStateMachine.py for the pattern; all
step logic lives in RealSegmentStateMachine."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class NcdexPhySegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "NCDEXPHY"
