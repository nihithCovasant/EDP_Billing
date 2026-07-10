"""
processes_json read/write helpers — per-state execution state for a
segment_execution row (see models.SegmentExecution docstring for shapes).

Every top-level processes_json key is exactly a SegmentState.value string —
the same vocabulary current_state uses — so there is exactly one way to
name "the RECON step", not two. Where a state does more than one
sub-operation (WAITING_FOR_FILE_UPLOAD does PID reservation on its first
entry, then polls FILEUPLOAD on every later one), that state's dict nests
the earlier sub-operation's result under its own key
(WAITING_FOR_FILE_UPLOAD["pid_reservation"]) rather than using a second
top-level key — insertion order still reads chronologically, since that
nested field is written before the poll_count/last_response fields that
share the same top-level dict ever appear.

TRIGGERED's dict has its own status progression for double-trigger crash
protection: TRIGGERING -> TRIGGERED (or FAILED). "TRIGGERING" is written
BEFORE the CBOS call, and is the signal a resumed pod uses to run the
recovery check instead of re-firing. TRIGGERED is shared verbatim by both
pipelines (see models.SegmentState).

Always use set_proc() to reassign the whole dict — SQLAlchemy doesn't
detect in-place mutations on JSON columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import SegmentExecution, SegmentState

# States whose stage completes with a "confirmed_at" timestamp
_CONFIRM_STAGES = {
    SegmentState.WAITING_FOR_BILLPOSTING.value,
    SegmentState.WAITING_FOR_RECON.value,
    SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION.value,
    SegmentState.WAITING_FOR_COMPLETION.value,
}
# States whose stage completes with a "ready_at" timestamp
_READY_STAGES = {SegmentState.WAITING_FOR_FILE_UPLOAD.value, SegmentState.WAITING_FOR_GTG.value}
# All others (INIT) get "checked_at"


def get_proc(row: SegmentExecution, stage_key: str) -> dict:
    """Return a copy of the stage state dict (empty dict if not yet written)."""
    return dict(row.processes_json.get(stage_key, {}))


def set_proc(row: SegmentExecution, stage_key: str, state: dict) -> None:
    """
    Write a stage state dict into processes_json.
    Reassigns the entire column dict so SQLAlchemy detects the mutation.
    """
    updated = dict(row.processes_json)
    updated[stage_key] = state
    row.processes_json = updated


def patch_proc(row: SegmentExecution, stage_key: str, **kwargs: Any) -> None:
    """Merge keyword arguments into an existing stage state dict."""
    state = get_proc(row, stage_key)
    state.update(kwargs)
    set_proc(row, stage_key, state)


def inc_poll(row: SegmentExecution, stage_key: str, last_response: str) -> None:
    """
    Increment the poll counter for a stage and record the latest CBOS response.
    Called on every file_process_status call that returns FALSE (still waiting).
    No "status" write here — current_state (on the row) is what drives
    control flow; this is purely a poll_count/last_response diagnostic log,
    left absent (not "POLLING") until the stage actually completes.
    """
    state = get_proc(row, stage_key)
    state["poll_count"] = state.get("poll_count", 0) + 1
    state["last_response"] = last_response
    set_proc(row, stage_key, state)


def mark_stage_done(
    row: SegmentExecution,
    stage_key: str,
    last_response: str,
    now: datetime,
) -> None:
    """
    Mark a stage as COMPLETED with the appropriate completion timestamp.
    Called when CBOS returns TRUE (or SKIP for INIT's holiday check).
    """
    state = get_proc(row, stage_key)
    state["status"] = "COMPLETED"
    state["last_response"] = last_response

    if stage_key in _CONFIRM_STAGES:
        state["confirmed_at"] = now.isoformat()
    elif stage_key in _READY_STAGES:
        state["ready_at"] = now.isoformat()
    else:
        state["checked_at"] = now.isoformat()

    # Preserve accumulated poll_count
    set_proc(row, stage_key, state)


def record_pid_reservation(
    row: SegmentExecution, process_id: str, source: str, now: datetime,
) -> None:
    """
    Record the PID-reservation outcome as a nested "pid_reservation" field
    inside WAITING_FOR_FILE_UPLOAD's own dict (its first-entry operation) —
    not a separate top-level key, so processes_json's top-level keys stay
    exactly the SegmentState vocabulary. Nesting still keeps insertion
    order chronological: this field is written before poll_count/
    last_response ever appear in the same dict (those come from the later
    FILEUPLOAD-poll entries).
    """
    patch_proc(row, SegmentState.WAITING_FOR_FILE_UPLOAD.value, pid_reservation={
        "status": "PID_RESERVED",
        "process_id_reserved": process_id,
        "process_id_source": source,
        "reserved_at": now.isoformat(),
    })


def record_trigger_attempt(row: SegmentExecution, now: datetime) -> None:
    """
    Pre-commit write — double-trigger protection (see
    state_machine.RealSegmentStateMachine.handle_triggered).
    Durably records intent BEFORE the CBOS call, so a crash before the
    outcome is recorded leaves "TRIGGERING" for the recovery check to see.
    Carries process_id_source forward from WAITING_FOR_FILE_UPLOAD's nested
    "pid_reservation" field into TRIGGERED's own dict, so it survives the
    eventual TRIGGERED/FAILED write.
    """
    pid_reservation = get_proc(row, SegmentState.WAITING_FOR_FILE_UPLOAD.value).get("pid_reservation", {})
    set_proc(row, SegmentState.TRIGGERED.value, {
        "status": "TRIGGERING",
        "attempt_started_at": now.isoformat(),
        "process_id_source": pid_reservation.get("process_id_source"),
    })


def record_trigger(
    row: SegmentExecution,
    process_id: str,
    is_runnable: bool,
    now: datetime,
) -> None:
    """Record a successful trigger call, preserving process_id_source
    ("EXISTING"|"RESERVED_NEW") carried forward via record_trigger_attempt()."""
    existing = get_proc(row, SegmentState.TRIGGERED.value)
    set_proc(row, SegmentState.TRIGGERED.value, {
        "status": "TRIGGERED",
        "at": now.isoformat(),
        "process_id_used": process_id,
        "process_id_source": existing.get("process_id_source"),
        "is_runnable": is_runnable,
    })


def record_trigger_failed(row: SegmentExecution, error: str, now: datetime) -> None:
    """
    Record a CONFIRMED, permanent trigger failure — only for non-transient
    failures, always paired with AbstractSegmentStateMachine._fail_result().
    Transient failures deliberately skip this, leaving "TRIGGERING" for recovery.
    """
    existing = get_proc(row, SegmentState.TRIGGERED.value)
    set_proc(row, SegmentState.TRIGGERED.value, {
        "status": "FAILED",
        "at": now.isoformat(),
        "error": error,
        "process_id_source": existing.get("process_id_source"),
    })


def record_post_trade_trigger_attempt(row: SegmentExecution, now: datetime) -> None:
    """
    Pre-commit crash-safety marker for post-trade triggers. Unlike the
    real-segment TRIGGERED step, there's no CBOS-side check afterwards, so
    this can't power an automatic re-trigger decision — it exists purely
    so a crash mid-call is durably visible; handle_triggered() refuses to
    re-fire when it sees this and requires manual verification instead.
    """
    set_proc(row, SegmentState.TRIGGERED.value, {
        "status": "TRIGGERING",
        "attempt_started_at": now.isoformat(),
    })


def record_post_trade_trigger(row: SegmentExecution, message: str, now: datetime) -> None:
    """Record a successful post-trade trigger call (no process_id involved)."""
    set_proc(row, SegmentState.TRIGGERED.value, {
        "status": "TRIGGERED",
        "at": now.isoformat(),
        "message": message,
    })


def record_post_trade_trigger_failed(row: SegmentExecution, error: str, now: datetime) -> None:
    """Record a failed post-trade trigger attempt."""
    set_proc(row, SegmentState.TRIGGERED.value, {
        "status": "FAILED",
        "at": now.isoformat(),
        "error": error,
    })
