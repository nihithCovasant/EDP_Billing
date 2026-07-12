"""
Proves/disproves a suspected crash bug in
src/agent/edp/utils/json_helpers.py: if row.processes_json[state_key] is a
non-dict (str, None, list) instead of a dict, calling .get("steps", {}) (or
similar dict methods) on it should raise an unhandled AttributeError,
crashing the whole wake cycle instead of failing just one segment.

None of get_state / get_step / set_step / record_poll / mark_step_done have
any isinstance/try-except defensive checks before operating on
processes_json[state_key]. But the exact failure mode differs by call path:

  - get_step() / record_poll() call `.get(state_key, {}).get("steps", {})`
    directly on the raw value -> always AttributeError for str/None/list
    (none of them have a `.get` method).
  - set_step() / mark_step_done() call get_state() first, which does
    `dict(row.processes_json.get(state_key, {}))` -- a coercion, not a
    guard. Its outcome depends on the exact malformed shape:
      * a plain string  -> ValueError ("dictionary update sequence...")
      * None            -> TypeError ("'NoneType' object is not iterable")
      * []  (empty list)-> dict([]) == {} -- NO crash; the malformed value
                           is silently discarded and overwritten.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.agent.edp.utils.json_helpers import (
    get_step,
    mark_step_done,
    record_poll,
    set_step,
)

STATE_KEY = "INIT"
STEP_KEY = "some_step"
NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)

MALFORMED_SHAPES = {
    "string": "this is a string, not a dict",
    "none": None,
    "list": [],
}


def make_row(processes_json: dict) -> SimpleNamespace:
    """Minimal fake row — json_helpers only ever touches .processes_json."""
    return SimpleNamespace(processes_json=processes_json)


# ---------------------------------------------------------------------------
# get_step
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape_name", MALFORMED_SHAPES)
def test_get_step_crashes_on_malformed_state(shape_name):
    row = make_row({STATE_KEY: MALFORMED_SHAPES[shape_name]})
    with pytest.raises(AttributeError):
        get_step(row, STATE_KEY, STEP_KEY)


# ---------------------------------------------------------------------------
# set_step
#
# set_step() calls get_state() first, which does `dict(row.processes_json
# .get(state_key, {}))` — a coercion, not a defensive isinstance check. Its
# behavior on a malformed value therefore depends on exactly what shape it
# is, and is NOT the plain AttributeError produced by get_step()/
# record_poll() (which go straight to `.get("steps", {})` on the raw value):
#   - a non-dict, non-iterable-of-pairs string -> dict(str) raises ValueError
#     ("dictionary update sequence element #0 has length 1; 2 is required")
#   - None -> dict(None) raises TypeError ("'NoneType' object is not iterable")
#   - [] (empty list) -> dict([]) succeeds silently and produces {} — no
#     crash at all; the malformed state is silently discarded/overwritten.
# ---------------------------------------------------------------------------

def test_set_step_string_state_raises_value_error():
    row = make_row({STATE_KEY: MALFORMED_SHAPES["string"]})
    with pytest.raises(ValueError):
        set_step(row, STATE_KEY, STEP_KEY, {"last_response": "TRUE"})


def test_set_step_none_state_raises_type_error():
    row = make_row({STATE_KEY: MALFORMED_SHAPES["none"]})
    with pytest.raises(TypeError):
        set_step(row, STATE_KEY, STEP_KEY, {"last_response": "TRUE"})


def test_set_step_list_state_does_not_crash_and_overwrites_silently():
    row = make_row({STATE_KEY: MALFORMED_SHAPES["list"]})
    # dict([]) == {} -- no crash, but the original malformed value is
    # silently dropped and replaced with a fresh state dict.
    set_step(row, STATE_KEY, STEP_KEY, {"last_response": "TRUE"})
    assert row.processes_json[STATE_KEY]["steps"][STEP_KEY] == {"last_response": "TRUE"}


# ---------------------------------------------------------------------------
# record_poll
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape_name", MALFORMED_SHAPES)
def test_record_poll_crashes_on_malformed_state(shape_name):
    row = make_row({STATE_KEY: MALFORMED_SHAPES[shape_name]})
    with pytest.raises(AttributeError):
        record_poll(row, STATE_KEY, STEP_KEY, "FALSE", NOW)


# ---------------------------------------------------------------------------
# mark_step_done
#
# Same reasoning as set_step(): it also starts with get_state(), whose
# `dict(...)` coercion determines the outcome per shape.
# ---------------------------------------------------------------------------

def test_mark_step_done_string_state_raises_value_error():
    row = make_row({STATE_KEY: MALFORMED_SHAPES["string"]})
    with pytest.raises(ValueError):
        mark_step_done(row, STATE_KEY, STEP_KEY, "TRUE", NOW)


def test_mark_step_done_none_state_raises_type_error():
    row = make_row({STATE_KEY: MALFORMED_SHAPES["none"]})
    with pytest.raises(TypeError):
        mark_step_done(row, STATE_KEY, STEP_KEY, "TRUE", NOW)


def test_mark_step_done_list_state_does_not_crash_and_overwrites_silently():
    row = make_row({STATE_KEY: MALFORMED_SHAPES["list"]})
    mark_step_done(row, STATE_KEY, STEP_KEY, "TRUE", NOW)
    state = row.processes_json[STATE_KEY]
    assert state["status"] == "COMPLETED"
    assert state["steps"][STEP_KEY]["last_response"] == "TRUE"
    assert state["steps"][STEP_KEY]["checked_at"] == NOW.isoformat()


# ---------------------------------------------------------------------------
# Healthy / normal cases — must NOT crash
# ---------------------------------------------------------------------------

def test_get_step_healthy_empty_processes_json():
    row = make_row({})
    assert get_step(row, STATE_KEY, STEP_KEY) == {}


def test_set_step_healthy_empty_processes_json():
    row = make_row({})
    set_step(row, STATE_KEY, STEP_KEY, {"last_response": "TRUE"})
    assert row.processes_json[STATE_KEY]["steps"][STEP_KEY] == {"last_response": "TRUE"}


def test_record_poll_healthy_empty_processes_json():
    row = make_row({})
    record_poll(row, STATE_KEY, STEP_KEY, "FALSE", NOW)
    step = row.processes_json[STATE_KEY]["steps"][STEP_KEY]
    assert step["last_response"] == "FALSE"
    assert step["last_checked_at"] == NOW.isoformat()


def test_mark_step_done_healthy_empty_processes_json():
    row = make_row({})
    mark_step_done(row, STATE_KEY, STEP_KEY, "TRUE", NOW)
    state = row.processes_json[STATE_KEY]
    assert state["status"] == "COMPLETED"
    assert state["steps"][STEP_KEY]["last_response"] == "TRUE"
    # INIT is not in _CONFIRM_STAGES or _READY_STAGES -> "checked_at"
    assert state["steps"][STEP_KEY]["checked_at"] == NOW.isoformat()
