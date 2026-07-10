"""
processes_json read/write helpers — per-stage execution state for a
segment_execution row (see models.SegmentExecution docstring for shapes).

The "trigger" stage has its own status progression for double-trigger
crash protection: PID_RESERVED -> TRIGGERING -> TRIGGERED (or FAILED).
"TRIGGERING" is written BEFORE the CBOS call, and is the signal a resumed
pod uses to run the recovery check instead of re-firing.

Always use set_proc() to reassign the whole dict — SQLAlchemy doesn't
detect in-place mutations on JSON columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import SegmentExecution

# Stages that end with a "confirmed_at" timestamp
_CONFIRM_STAGES = {"bill_posting", "recon", "contract_note", "confirm"}
# Stages that end with a "ready_at" timestamp
_READY_STAGES = {"file_upload_ready", "gtg"}
# All others get "checked_at"


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
    Called when CBOS returns TRUE (or SKIP for holiday_check).
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
    Record the PID-reservation outcome under its own "pid_reservation" key
    (WAITING_FOR_FILE_UPLOAD's first-entry operation) — kept separate from
    "trigger" so processes_json key insertion order matches the actual
    pipeline order (pid_reservation happens, and is logged, before
    file_upload_ready even exists; "trigger" isn't created until TRIGGERED
    genuinely fires).
    """
    set_proc(row, "pid_reservation", {
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
    Carries process_id_source forward from the "pid_reservation" stage into
    "trigger" itself, so it survives the eventual TRIGGERED/FAILED write.
    """
    pid_reservation = get_proc(row, "pid_reservation")
    set_proc(row, "trigger", {
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
    ("EXISTING"|"RESERVED_NEW") set by WAITING_FOR_FILE_UPLOAD's
    PID-reservation step."""
    existing = get_proc(row, "trigger")
    set_proc(row, "trigger", {
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
    existing = get_proc(row, "trigger")
    set_proc(row, "trigger", {
        "status": "FAILED",
        "at": now.isoformat(),
        "error": error,
        "process_id_source": existing.get("process_id_source"),
    })


def record_post_trade_trigger_attempt(row: SegmentExecution, now: datetime) -> None:
    """
    Pre-commit crash-safety marker for post-trade triggers. Unlike the
    real-segment TRIGGER step, there's no CBOS-side check afterwards, so
    this can't power an automatic re-trigger decision — it exists purely
    so a crash mid-call is durably visible; handle_trigger_job() refuses
    to re-fire when it sees this and requires manual verification instead.
    """
    set_proc(row, "trigger", {
        "status": "TRIGGERING",
        "attempt_started_at": now.isoformat(),
    })


def record_post_trade_trigger(row: SegmentExecution, message: str, now: datetime) -> None:
    """Record a successful post-trade trigger call (no process_id involved)."""
    set_proc(row, "trigger", {
        "status": "TRIGGERED",
        "at": now.isoformat(),
        "message": message,
    })


def record_post_trade_trigger_failed(row: SegmentExecution, error: str, now: datetime) -> None:
    """Record a failed post-trade trigger attempt."""
    set_proc(row, "trigger", {
        "status": "FAILED",
        "at": now.isoformat(),
        "error": error,
    })
