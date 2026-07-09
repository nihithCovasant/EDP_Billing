"""MTF Fund Transfer post-trade process (MTFFT) — see ColValStateMachine.py
for the pattern; shared logic lives in PostTradeStateMachine."""

from __future__ import annotations

from ..PostTradeStateMachine import PostTradeStateMachine


class MtfFtStateMachine(PostTradeStateMachine):
    SEGMENT_CODE = "MTFFT"
    TRIGGER_METHOD_NAME = "trigger_mtf_fund_transfer"
