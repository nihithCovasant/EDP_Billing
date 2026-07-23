"""
Unit tests for the processes_json read/write helpers in
src.agent.edp.utils.json_helpers. These functions only ever touch a plain
dict attribute (row.processes_json) so they're tested here against a bare
fake row object - no DB, no SQLAlchemy, no async.

Coverage:
- get_state/set_state round-trip and the "always a copy" contract (mutating
  a returned dict must not leak back into the row).
- set_step's merge behavior: writing one step must not clobber sibling keys
  already present in that state's dict (e.g. "status").
- record_poll never writes a state-level "status" - current_state on the
  row (not processes_json) drives control flow, so this must stay a pure
  diagnostic log.
- mark_step_done's three different completion-timestamp names depending on
  which stage bucket the state falls into (_CONFIRM_STAGES/_READY_STAGES/
  neither), plus the "status": "COMPLETED" write.
- PID reservation round-trip through WAITING_FOR_FILE_UPLOAD's nested step.
- The trigger/post-trade-trigger attempt -> outcome trio: regression
  coverage for a previously-fixed bug where attempt_started_at and
  process_id_source were dropped between the "TRIGGERING" pre-commit write
  and the final TRIGGERED/FAILED write instead of being carried forward.
- Calling record_trigger()/record_trigger_failed() with no prior
  record_trigger_attempt() must not crash - carried-forward fields just
  come back as None.
"""

from __future__ import annotations

from datetime import datetime

from src.agent.edp.utils.json_helpers import (
    get_pid_reservation,
    get_state,
    get_step,
    mark_step_done,
    record_pid_reservation,
    record_poll,
    record_post_trade_trigger,
    record_post_trade_trigger_attempt,
    record_post_trade_trigger_failed,
    record_trigger,
    record_trigger_attempt,
    record_trigger_failed,
    set_state,
    set_step,
)
from src.agent.edp.models import SegmentState


class FakeRow:
    def __init__(self):
        self.processes_json = {}


NOW = datetime(2026, 7, 11, 12, 0, 0)


def test_get_state_on_missing_key_returns_empty_dict():
    row = FakeRow()
    assert get_state(row, SegmentState.INIT.value) == {}


def test_set_state_then_get_state_round_trips():
    row = FakeRow()
    set_state(row, SegmentState.INIT.value, {"status": "COMPLETED"})
    assert get_state(row, SegmentState.INIT.value) == {"status": "COMPLETED"}


def test_get_state_returns_a_copy_not_a_live_reference():
    row = FakeRow()
    set_state(row, SegmentState.INIT.value, {"status": "COMPLETED"})
    fetched = get_state(row, SegmentState.INIT.value)
    fetched["status"] = "MUTATED"
    fetched["new_key"] = "leaked?"
    assert get_state(row, SegmentState.INIT.value) == {"status": "COMPLETED"}


def test_set_step_preserves_existing_sibling_fields_in_state_dict():
    row = FakeRow()
    set_state(row, SegmentState.INIT.value, {"status": "COMPLETED", "steps": {}})
    set_step(row, SegmentState.INIT.value, "SOME_STEP", {"last_response": "TRUE"})
    state = get_state(row, SegmentState.INIT.value)
    assert state["status"] == "COMPLETED"
    assert state["steps"]["SOME_STEP"] == {"last_response": "TRUE"}


def test_get_step_on_missing_step_returns_empty_dict():
    row = FakeRow()
    assert get_step(row, SegmentState.INIT.value, "SOME_STEP") == {}


def test_record_poll_writes_last_response_and_last_checked_at_only():
    row = FakeRow()
    record_poll(row, SegmentState.INIT.value, "FILEUPLOAD_STATUS", "FALSE", NOW)
    step = get_step(row, SegmentState.INIT.value, "FILEUPLOAD_STATUS")
    assert step["last_response"] == "FALSE"
    assert step["last_checked_at"] == NOW.isoformat()


def test_record_poll_does_not_write_state_level_status():
    row = FakeRow()
    record_poll(row, SegmentState.INIT.value, "FILEUPLOAD_STATUS", "FALSE", NOW)
    state = get_state(row, SegmentState.INIT.value)
    assert "status" not in state


def test_mark_step_done_on_confirm_stage_writes_confirmed_at():
    row = FakeRow()
    mark_step_done(row, SegmentState.WAITING_FOR_BILLPOSTING.value, "BILLPOSTING_STATUS", "TRUE", NOW)
    step = get_step(row, SegmentState.WAITING_FOR_BILLPOSTING.value, "BILLPOSTING_STATUS")
    assert step["confirmed_at"] == NOW.isoformat()
    assert "ready_at" not in step
    assert "checked_at" not in step


def test_mark_step_done_on_ready_stage_writes_ready_at():
    row = FakeRow()
    mark_step_done(row, SegmentState.WAITING_FOR_FILE_UPLOAD.value, "FILEUPLOAD_STATUS", "TRUE", NOW)
    step = get_step(row, SegmentState.WAITING_FOR_FILE_UPLOAD.value, "FILEUPLOAD_STATUS")
    assert step["ready_at"] == NOW.isoformat()
    assert "confirmed_at" not in step
    assert "checked_at" not in step


def test_mark_step_done_on_init_writes_checked_at():
    row = FakeRow()
    mark_step_done(row, SegmentState.INIT.value, "HOLIDAY_CHECK", "SKIP", NOW)
    step = get_step(row, SegmentState.INIT.value, "HOLIDAY_CHECK")
    assert step["checked_at"] == NOW.isoformat()
    assert "confirmed_at" not in step
    assert "ready_at" not in step


def test_mark_step_done_sets_state_status_completed():
    row = FakeRow()
    mark_step_done(row, SegmentState.INIT.value, "HOLIDAY_CHECK", "SKIP", NOW)
    state = get_state(row, SegmentState.INIT.value)
    assert state["status"] == "COMPLETED"


def test_pid_reservation_round_trip():
    row = FakeRow()
    record_pid_reservation(row, "PID123", "EXISTING", NOW)
    reservation = get_pid_reservation(row)
    assert reservation["process_id_reserved"] == "PID123"
    assert reservation["process_id_source"] == "EXISTING"
    assert reservation["reserved_at"] == NOW.isoformat()


def test_record_trigger_carries_forward_process_id_source_and_attempt_started_at():
    """Regression coverage: these two fields must survive from the
    TRIGGERING pre-commit write into the final TRIGGERED dict."""
    row = FakeRow()
    record_pid_reservation(row, "PID123", "EXISTING", NOW)
    record_trigger_attempt(row, NOW)

    later = datetime(2026, 7, 11, 12, 5, 0)
    record_trigger(row, "PID123", True, later)

    state = get_state(row, SegmentState.TRIGGERED.value)
    assert state["status"] == "TRIGGERED"
    assert state["attempt_started_at"] == NOW.isoformat()
    assert state["process_id_source"] == "EXISTING"
    assert state["process_id_used"] == "PID123"
    assert state["is_runnable"] is True
    assert state["at"] == later.isoformat()


def test_record_trigger_failed_carries_forward_fields_and_sets_status_failed():
    row = FakeRow()
    record_pid_reservation(row, "PID123", "EXISTING", NOW)
    record_trigger_attempt(row, NOW)

    later = datetime(2026, 7, 11, 12, 5, 0)
    record_trigger_failed(row, "CBOS rejected", later)

    state = get_state(row, SegmentState.TRIGGERED.value)
    assert state["status"] == "FAILED"
    assert state["attempt_started_at"] == NOW.isoformat()
    assert state["process_id_source"] == "EXISTING"
    assert state["error"] == "CBOS rejected"
    assert state["at"] == later.isoformat()


def test_record_post_trade_trigger_carries_forward_attempt_started_at_and_sets_message():
    row = FakeRow()
    record_post_trade_trigger_attempt(row, NOW)

    later = datetime(2026, 7, 11, 12, 5, 0)
    record_post_trade_trigger(row, "trigger confirmed", later)

    state = get_state(row, SegmentState.TRIGGERED.value)
    assert state["status"] == "TRIGGERED"
    assert state["attempt_started_at"] == NOW.isoformat()
    assert state["message"] == "trigger confirmed"
    assert state["at"] == later.isoformat()


def test_record_post_trade_trigger_failed_carries_forward_attempt_started_at_and_sets_error():
    row = FakeRow()
    record_post_trade_trigger_attempt(row, NOW)

    later = datetime(2026, 7, 11, 12, 5, 0)
    record_post_trade_trigger_failed(row, "timed out", later)

    state = get_state(row, SegmentState.TRIGGERED.value)
    assert state["status"] == "FAILED"
    assert state["attempt_started_at"] == NOW.isoformat()
    assert state["error"] == "timed out"
    assert state["at"] == later.isoformat()


def test_record_trigger_without_prior_attempt_does_not_crash():
    """No record_trigger_attempt() was called first, so get_state() returns
    {} - carried-forward fields must fall back to None via .get(), not raise."""
    row = FakeRow()
    record_trigger(row, "PID999", False, NOW)
    state = get_state(row, SegmentState.TRIGGERED.value)
    assert state["attempt_started_at"] is None
    assert state["process_id_source"] is None
    assert state["status"] == "TRIGGERED"


def test_record_trigger_failed_without_prior_attempt_does_not_crash():
    row = FakeRow()
    record_trigger_failed(row, "boom", NOW)
    state = get_state(row, SegmentState.TRIGGERED.value)
    assert state["attempt_started_at"] is None
    assert state["process_id_source"] is None
    assert state["status"] == "FAILED"
