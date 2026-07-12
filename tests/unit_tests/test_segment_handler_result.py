"""
Unit tests for the SegmentHandlerResult dataclass and its outcome string
constants in src.agent.edp.state_machine.SegmentHandlerResult.

Coverage:
- Each outcome constant (ADVANCE/BLOCKED/COMPLETED/SKIPPED/FAILED) has its
  documented literal value - regression coverage, since these strings are
  almost certainly compared/logged elsewhere and an accidental rename of
  the literal (as opposed to the constant name) would be a silent bug.
- SegmentHandlerResult can be constructed with only "outcome" and every
  other field defaults to None.
- Constructing with every field set round-trips correctly via attribute
  access.
"""

from __future__ import annotations

from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.state_machine.SegmentHandlerResult import (
    ADVANCE,
    BLOCKED,
    COMPLETED,
    FAILED,
    SKIPPED,
    SegmentHandlerResult,
)


def test_advance_constant_value():
    assert ADVANCE == "advanced"


def test_blocked_constant_value():
    assert BLOCKED == "blocked"


def test_completed_constant_value():
    assert COMPLETED == "completed"


def test_skipped_constant_value():
    assert SKIPPED == "skipped"


def test_failed_constant_value():
    assert FAILED == "failed"


def test_construct_with_only_outcome_defaults_rest_to_none():
    result = SegmentHandlerResult(outcome=ADVANCE)
    assert result.outcome == ADVANCE
    assert result.next_state is None
    assert result.next_process is None
    assert result.next_status is None
    assert result.category is None
    assert result.reason is None


def test_construct_with_all_fields_round_trips():
    result = SegmentHandlerResult(
        outcome=FAILED,
        next_state=SegmentState.INIT,
        next_process="MTFCOLLALLOC",
        next_status=SegmentStatus.FAILED,
        category="TRANSIENT",
        reason="CBOS timed out",
    )
    assert result.outcome == FAILED
    assert result.next_state == SegmentState.INIT
    assert result.next_process == "MTFCOLLALLOC"
    assert result.next_status == SegmentStatus.FAILED
    assert result.category == "TRANSIENT"
    assert result.reason == "CBOS timed out"
