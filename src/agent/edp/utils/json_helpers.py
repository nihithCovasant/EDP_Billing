"""
processes_json read/write helpers.

processes_json stores the 6-stage execution state for a segment:
  holiday_check    — BeginFileUpload status check result
  file_upload_ready — FILEUPLOAD poll result
  trigger          — getNewTradeProcess trigger record
  bill_posting     — BILLPOSTING poll result
  recon            — RECON poll result
  contract_note    — CONTRACTNOTEGENERATION poll result

SQLAlchemy does NOT detect in-place mutations on JSON columns.
Always use set_proc() to reassign the top-level dict so the ORM marks
the column as modified.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import SegmentExecution

# Stages that end with a "confirmed_at" timestamp
_CONFIRM_STAGES = {"bill_posting", "recon", "contract_note"}
# Stages that end with a "ready_at" timestamp
_READY_STAGES = {"file_upload_ready"}
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


def record_trigger(
    row: SegmentExecution,
    process_id: str,
    is_runnable: bool,
    now: datetime,
) -> None:
    """Record a successful getNewTradeProcess trigger call."""
    set_proc(row, "trigger", {
        "status": "TRIGGERED",
        "at": now.isoformat(),
        "process_id_used": process_id,
        "is_runnable": is_runnable,
    })


def record_trigger_failed(row: SegmentExecution, error: str, now: datetime) -> None:
    """Record a failed getNewTradeProcess trigger attempt."""
    set_proc(row, "trigger", {
        "status": "FAILED",
        "at": now.isoformat(),
        "error": error,
    })
