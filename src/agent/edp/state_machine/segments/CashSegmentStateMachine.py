"""Cash segment (EQ) — 7-step pipeline behavior is identical for all real
segments and lives in RealSegmentStateMachine; this file only identifies
which segment_code it drives."""

from __future__ import annotations

from ..RealSegmentStateMachine import RealSegmentStateMachine


class CashSegmentStateMachine(RealSegmentStateMachine):
    SEGMENT_CODE = "EQ"
