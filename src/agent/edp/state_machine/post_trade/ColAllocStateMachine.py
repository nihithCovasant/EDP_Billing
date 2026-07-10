"""Collateral Allocation post-trade process (COLALLOC) — see
ColValStateMachine.py for the pattern; shared logic lives in
PostTradeStateMachine."""

from __future__ import annotations

from ..PostTradeStateMachine import PostTradeStateMachine


class ColAllocStateMachine(PostTradeStateMachine):
    SEGMENT_CODE = "COLALLOC"
    TRIGGER_METHOD_NAME = "trigger_collateral_allocation"
    CHECK_TRIGGERED_METHOD_NAME = "check_collateral_allocation_triggered"
