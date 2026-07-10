"""Daily Margin Statements post-trade process (DMSTMT) — see
ColValStateMachine.py for the pattern; shared logic lives in
PostTradeStateMachine."""

from __future__ import annotations

from ..PostTradeStateMachine import PostTradeStateMachine


class DmStmtStateMachine(PostTradeStateMachine):
    SEGMENT_CODE = "DMSTMT"
    TRIGGER_METHOD_NAME = "trigger_daily_margin_statements"
    CHECK_TRIGGERED_METHOD_NAME = "check_daily_margin_statements_triggered"
