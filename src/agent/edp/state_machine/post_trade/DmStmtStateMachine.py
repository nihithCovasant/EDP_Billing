"""Daily Margin Statements post-trade process (DMSTMT) — see
ColValStateMachine.py for the pattern; shared logic lives in
PostTradeStateMachine."""

from __future__ import annotations

from ..PostTradeStateMachine import PostTradeStateMachine


class DmStmtStateMachine(PostTradeStateMachine):
    SEGMENT_CODE = "DMSTMT"
    TRIGGER_METHOD_NAME = "trigger_daily_margin_statements"
    CHECK_TRIGGERED_METHOD_NAME = "check_daily_margin_statements_triggered"
    # No CBOS GTG/holiday-check endpoint exists for DMSTMT — its readiness
    # gate is "DMRPT (previous in POST_TRADE_ORDER) reached a terminal DB
    # status", checked in PostTradeStateMachine._check_previous_process_terminal().
    DEPENDS_ON_PREVIOUS_PROCESS = True
