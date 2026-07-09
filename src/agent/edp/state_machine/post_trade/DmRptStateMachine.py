"""Daily Margin Reporting post-trade process (DMRPT) — see
ColValStateMachine.py for the pattern; shared logic lives in
PostTradeStateMachine."""

from __future__ import annotations

from ..PostTradeStateMachine import PostTradeStateMachine


class DmRptStateMachine(PostTradeStateMachine):
    SEGMENT_CODE = "DMRPT"
    TRIGGER_METHOD_NAME = "trigger_daily_margin_reporting"
