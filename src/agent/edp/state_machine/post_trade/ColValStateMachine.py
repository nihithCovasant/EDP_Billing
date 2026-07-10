"""Collateral Valuation post-trade process (COLVAL) — the GTG/confirm poll
and state machine are shared in PostTradeStateMachine; this file only
identifies the segment_code and which CbosClient trigger method to fire."""

from __future__ import annotations

from ..PostTradeStateMachine import PostTradeStateMachine


class ColValStateMachine(PostTradeStateMachine):
    SEGMENT_CODE = "COLVAL"
    TRIGGER_METHOD_NAME = "trigger_collateral_valuation"
    CHECK_TRIGGERED_METHOD_NAME = "check_collateral_valuation_triggered"
