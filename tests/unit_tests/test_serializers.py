"""
Unit tests for the SegmentExecution -> dict serialization helpers in
src/agent/edp/utils/serializers.py — no DB, no async. Rows are built as
real (non-mock) SegmentExecution instances, since the model is a plain
SQLAlchemy declarative class constructible without a session/engine.

- serialize_segment()/serialize_segment_summary() must unwrap enum columns
  to their .value strings (segment_status, current_state) rather than
  leaking the enum object itself into the API response, and must render
  datetimes as ISO strings via the shared _dt() helper — a raw datetime or
  enum object in a dict eventually breaks JSON serialization one layer up.
- current_state is Optional (a row can have segment_status=COMPLETED with
  current_state reset to None) — the ternary in the source must produce
  Python None, not the string "None".
- _runtime_health()'s three-condition `and` chain is the one bit of actual
  logic in this module: it must special-case "only IN_PROGRESS rows can be
  STALE" and "a missing heartbeat is never STALE", both of which are easy
  to get backwards if the conditions are reordered or an `and` becomes an
  `or`.
- processes_json's `row.processes_json or {}` fallback exists because the
  column, while nullable=False, still permits an empty/falsy dict; the
  test constructs with {} explicitly rather than fighting nullable=False
  with None.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.agent.edp.models import SegmentExecution, SegmentState, SegmentStatus
from src.agent.edp.utils.constants import (
    STALE_HEARTBEAT_THRESHOLD,
    get_segment_name,
    get_sequence_order,
)
from src.agent.edp.utils.datetime_utils import now_ist
from src.agent.edp.utils.serializers import (
    _runtime_health,
    serialize_segment,
    serialize_segment_alert,
    serialize_segment_summary,
)

IST = ZoneInfo("Asia/Kolkata")


def _full_row(**overrides) -> SegmentExecution:
    """A SegmentExecution with every field set to a concrete, non-null
    value (except created_at/updated_at, which are server-default-only and
    not set by the plain Python constructor)."""
    now = now_ist()
    defaults = {
        "id": "row-1",
        "trade_date": date(2026, 6, 29),
        "segment_code": "EQ",
        "config_id_used": "cfg-1",
        "segment_status": SegmentStatus.IN_PROGRESS,
        "current_process": "BeginFileUpload",
        "current_state": SegmentState.INIT,
        "process_id": "PID123",
        "process_id_reserved_at": now,
        "processes_json": {"INIT": {"status": "COMPLETED"}},
        "started_at": now,
        "completed_at": now,
        "last_heartbeat_at": now,
        "skip_category": "CBOS_SKIP",
        "skip_reason": "Holiday",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return SegmentExecution(**defaults)


def test_serialize_segment_returns_all_documented_keys_with_expected_values():
    row = _full_row()
    result = serialize_segment(row)

    assert result["id"] == "row-1"
    assert result["trade_date"] == date(2026, 6, 29).isoformat()
    assert result["segment_code"] == "EQ"
    assert result["segment_name"] == get_segment_name("EQ")
    assert result["sequence_order"] == get_sequence_order("EQ")
    assert result["segment_status"] == "IN_PROGRESS"
    assert isinstance(row.segment_status, SegmentStatus)  # sanity: still a real enum on the row
    assert result["current_process"] == "BeginFileUpload"
    assert result["current_state"] == "INIT"
    assert result["process_id"] == "PID123"
    assert result["process_id_reserved_at"] == row.process_id_reserved_at.isoformat()
    assert result["skip_category"] == "CBOS_SKIP"
    assert result["skip_reason"] == "Holiday"
    assert result["started_at"] == row.started_at.isoformat()
    assert result["completed_at"] == row.completed_at.isoformat()
    assert result["last_heartbeat_at"] == row.last_heartbeat_at.isoformat()
    assert result["runtime_health"] in ("STALE", "ACTIVE")
    assert result["config_id_used"] == "cfg-1"
    assert result["processes_json"] == {"INIT": {"status": "COMPLETED"}}
    assert result["created_at"] == row.created_at.isoformat()
    assert result["updated_at"] == row.updated_at.isoformat()

    expected_keys = {
        "id",
        "trade_date",
        "segment_code",
        "segment_name",
        "sequence_order",
        "segment_status",
        "current_process",
        "current_state",
        "process_id",
        "process_id_reserved_at",
        "skip_category",
        "skip_reason",
        "started_at",
        "completed_at",
        "last_heartbeat_at",
        "runtime_health",
        "config_id_used",
        "processes_json",
        "created_at",
        "updated_at",
    }
    assert set(result.keys()) == expected_keys


def test_serialize_segment_enum_columns_are_unwrapped_to_plain_strings():
    """segment_status/current_state must come out as their .value strings,
    not the enum object — a bare enum would break naive json.dumps() one
    layer up in the API response path."""
    row = _full_row(segment_status=SegmentStatus.COMPLETED, current_state=SegmentState.TRIGGERED)
    result = serialize_segment(row)
    assert result["segment_status"] == "COMPLETED"
    assert isinstance(result["segment_status"], str)
    assert result["current_state"] == "TRIGGERED"
    assert isinstance(result["current_state"], str)


def test_serialize_segment_current_state_none_serializes_to_none_not_string():
    """A row with segment_status=COMPLETED typically has current_state
    reset to None — the source's ternary must produce real None, not the
    string "None", and must not crash on the None.value access."""
    row = _full_row(segment_status=SegmentStatus.COMPLETED, current_state=None)
    result = serialize_segment(row)
    assert result["current_state"] is None
    assert result["current_state"] != "None"


def test_runtime_health_stale_when_in_progress_and_heartbeat_old():
    old_heartbeat = now_ist() - STALE_HEARTBEAT_THRESHOLD - timedelta(minutes=1)
    row = _full_row(segment_status=SegmentStatus.IN_PROGRESS, last_heartbeat_at=old_heartbeat)
    assert _runtime_health(row) == "STALE"


def test_runtime_health_active_when_in_progress_and_heartbeat_recent():
    recent_heartbeat = now_ist() - timedelta(minutes=1)
    row = _full_row(segment_status=SegmentStatus.IN_PROGRESS, last_heartbeat_at=recent_heartbeat)
    assert _runtime_health(row) == "ACTIVE"


def test_runtime_health_active_when_not_in_progress_even_with_very_old_heartbeat():
    """A COMPLETED (or any non-IN_PROGRESS) row is never STALE regardless
    of heartbeat age — the `and` chain in _runtime_health requires
    segment_status == IN_PROGRESS as a precondition. This guards against
    that condition being dropped or turned into an `or`."""
    ancient_heartbeat = now_ist() - timedelta(days=30)
    row = _full_row(segment_status=SegmentStatus.COMPLETED, last_heartbeat_at=ancient_heartbeat)
    assert _runtime_health(row) == "ACTIVE"


def test_runtime_health_active_when_no_heartbeat_yet_even_if_in_progress():
    """A row with last_heartbeat_at=None (never had a heartbeat) must not
    crash the `now_ist() - ensure_aware(...)` subtraction, and must read
    as ACTIVE, not STALE."""
    row = _full_row(segment_status=SegmentStatus.IN_PROGRESS, last_heartbeat_at=None)
    assert _runtime_health(row) == "ACTIVE"


def test_serialize_segment_summary_returns_compact_key_set():
    row = _full_row()
    result = serialize_segment_summary(row)

    expected_keys = {
        "segment_code",
        "segment_name",
        "sequence_order",
        "segment_status",
        "current_process",
        "current_state",
        "process_id",
        "process_id_reserved_at",
        "skip_category",
        "skip_reason",
        "started_at",
        "completed_at",
        "last_heartbeat_at",
        "runtime_health",
        "processes_json",
    }
    assert set(result.keys()) == expected_keys
    assert "id" not in result
    assert "config_id_used" not in result
    assert "created_at" not in result
    assert "updated_at" not in result


def test_serialize_segment_summary_shared_fields_match_full_serialize():
    """For the same row, every key serialize_segment_summary() does
    include must match what serialize_segment() produces for that key."""
    row = _full_row()
    full = serialize_segment(row)
    summary = serialize_segment_summary(row)
    for key in summary:
        assert summary[key] == full[key]


def test_serialize_segment_alert_drops_internal_audit_fields():
    """
    Regression test for the alert email leaking internal/audit fields as
    extra table columns (id, config_id_used, process_id_reserved_at,
    last_heartbeat_at, runtime_health, processes_json, created_at,
    updated_at, skip_category, sequence_order) — serialize_segment_alert()
    must be the lean, ops-facing subset, not serialize_segment()'s full
    detail shape.
    """
    row = _full_row()
    result = serialize_segment_alert(row)

    expected_keys = {
        "trade_date",
        "segment_code",
        "segment_name",
        "segment_status",
        "current_process",
        "current_state",
        "process_id",
        "skip_reason",
        "started_at",
        "completed_at",
    }
    assert set(result.keys()) == expected_keys

    for dropped in (
        "id",
        "config_id_used",
        "process_id_reserved_at",
        "last_heartbeat_at",
        "runtime_health",
        "processes_json",
        "created_at",
        "updated_at",
        "skip_category",
        "sequence_order",
    ):
        assert dropped not in result


def test_serialize_segment_alert_timestamps_are_short_ist_form_not_full_iso():
    """
    Alert timestamps must be human-short ('YYYY-MM-DD HH:MM:SS IST'), not
    serialize_segment()'s full ISO 8601 with microseconds + numeric offset
    (e.g. '2026-06-29T10:55:41.112136+05:30') — the agent only ever runs
    in IST, so the offset is implied, not informative, on every row.
    """
    row = _full_row(started_at=datetime(2026, 6, 29, 10, 55, 41, 112136, tzinfo=IST))
    result = serialize_segment_alert(row)
    assert result["started_at"] == "2026-06-29 10:55:41 IST"
    assert "+05:30" not in result["started_at"]
    assert "." not in result["started_at"]


def test_serialize_segment_alert_shared_fields_match_full_serialize_except_timestamps():
    """Non-timestamp fields must be identical values to serialize_segment()
    for the same row — only started_at/completed_at differ in format."""
    row = _full_row()
    full = serialize_segment(row)
    alert = serialize_segment_alert(row)
    for key in alert:
        if key in ("started_at", "completed_at"):
            continue
        assert alert[key] == full[key]


def test_processes_json_empty_dict_round_trips_unchanged():
    """processes_json is nullable=False, so the falsy value actually
    constructible here is {} (not None) — confirm the `row.processes_json
    or {}` fallback leaves an already-empty dict as {} in the output."""
    row = _full_row(processes_json={})
    result = serialize_segment(row)
    assert result["processes_json"] == {}

    summary = serialize_segment_summary(row)
    assert summary["processes_json"] == {}
