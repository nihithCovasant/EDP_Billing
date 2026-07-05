"""
processes_json read/write helpers.

processes_json stores the per-stage execution state for a segment_execution
row. Two shapes exist, depending on segment_code (see models.SegmentExecution
docstring for the full shapes):

  7 real segments (6 stages): holiday_check, file_upload_ready, trigger,
    bill_posting, recon, contract_note
  5 post-trade processes (3 stages): gtg, trigger, confirm

The "trigger" stage (real segments only) has its own internal status
progression, used for double-trigger crash protection — see
record_trigger_attempt() / pipeline.stages.handle_trigger():
  PID_RESERVED -> TRIGGERING -> TRIGGERED (or FAILED)
"TRIGGERING" is written BEFORE the CBOS call is made and is the only
status a resumed pod uses to decide it must run the recovery check instead
of firing the trigger again.

SQLAlchemy does NOT detect in-place mutations on JSON columns.
Always use set_proc() to reassign the top-level dict so the ORM marks
the column as modified.
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
    """
    state = get_proc(row, stage_key)
    state["status"] = "POLLING"
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


def record_trigger_attempt(row: SegmentExecution, now: datetime) -> None:
    """
    Pre-commit write — double-trigger protection (see pipeline.stages.handle_trigger).

    Durably records intent to call getNewTradeProcess(trigger) BEFORE the
    CBOS call is made, so the DB always leads the CBOS call and never
    follows it. If the pod dies anywhere between this write and the
    eventual record_trigger()/record_trigger_failed() write, the next wake
    cycle sees processes_json["trigger"]["status"] still "TRIGGERING" and
    runs the recovery decision tree instead of blindly re-firing the
    trigger — the one call that must never be repeated by accident.

    Preserves process_id_source (set by Step 2's RESERVE_PID stage) like
    record_trigger() does, since this is the same processes_json key.
    """
    existing = get_proc(row, "trigger")
    set_proc(row, "trigger", {
        "status": "TRIGGERING",
        "attempt_started_at": now.isoformat(),
        "process_id_source": existing.get("process_id_source"),
    })


def record_trigger(
    row: SegmentExecution,
    process_id: str,
    is_runnable: bool,
    now: datetime,
) -> None:
    """
    Record a successful getNewTradeProcess trigger call.

    Preserves process_id_source (set by Step 2's RESERVE_PID stage —
    "EXISTING" or "RESERVED_NEW") instead of overwriting the whole
    "trigger" state, since this is the same processes_json key.
    """
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
    Record a CONFIRMED, permanent getNewTradeProcess trigger failure.

    Only called for a definitive (non-transient) failure — always paired
    with pipeline.stages._fail(), which marks the whole segment FAILED (a
    terminal state that stops the pipeline from ever re-entering
    handle_trigger for this row). Transient failures deliberately do NOT
    call this — they leave processes_json["trigger"]["status"] as
    "TRIGGERING" so the next cycle goes through the recovery check instead.
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
    Pre-commit write — crash-safety marker for post-trade triggers (see
    pipeline.post_trade_stages.handle_trigger_job).

    Unlike the real-segment TRIGGER step, post-trade CBOS trigger endpoints
    have no PROCESSID/Table2 equivalent to check afterwards — there is no
    way to ask CBOS "did you actually get my last call?". So this marker
    cannot power an automatic re-trigger-if-safe decision the way
    record_trigger_attempt() does; it exists purely so a crash between the
    CBOS call and the outcome being recorded is durably visible (never
    silently reverts to "never attempted"), and handle_trigger_job() refuses
    to re-fire automatically when it sees this — see that function for the
    resulting "mark FAILED, require manual verification" behaviour.
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
