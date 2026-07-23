"""
processes_json read/write helpers — per-state execution state for a
segment_execution row (see models.SegmentExecution docstring for shapes).

Every top-level processes_json key is exactly a SegmentState.value string —
the same vocabulary current_state uses. Each state's dict has:

  {
    "status": "COMPLETED" | "TRIGGERING" | "TRIGGERED" | "FAILED",
    "steps": {
        "<EndpointName>_STATUS": {"last_response": ..., "last_checked_at"/
                                   "checked_at"/"ready_at"/"confirmed_at": ...},
        ...
    },
  }

"steps" holds one entry per distinct CBOS call that state makes, keyed by
a name that identifies exactly which endpoint/operation it recorded — so a
state that makes more than one kind of call (WAITING_FOR_FILE_UPLOAD does
PID reservation once, then polls FILEUPLOAD repeatedly) is still fully
self-describing without needing a second top-level key. Insertion order
within "steps" reads chronologically. TRIGGERED is the one state that
stays flat (no "steps" wrapper) — it's already a single atomic action, and
its dict carries its own crash-safety status progression: TRIGGERING ->
TRIGGERED (or FAILED). "TRIGGERING" is written BEFORE the CBOS call, and is
the signal a resumed pod uses to run the recovery check instead of
re-firing. TRIGGERED is shared verbatim by both pipelines (see
models.SegmentState).

No poll_count is tracked — last_response + a timestamp is enough to see
the latest observed CBOS state; how many times it was polled isn't useful
once the row is inspected after the fact.

Always use set_state()/set_step() to reassign the whole processes_json
dict — SQLAlchemy doesn't detect in-place mutations on JSON columns.
"""

from __future__ import annotations

from datetime import datetime

from ..models import SegmentExecution, SegmentState

# States whose step completes with a "confirmed_at" timestamp
_CONFIRM_STAGES = {
    SegmentState.WAITING_FOR_BILLPOSTING.value,
    SegmentState.WAITING_FOR_RECON.value,
    SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION.value,
    SegmentState.WAITING_FOR_COMPLETION.value,
}
# States whose step completes with a "ready_at" timestamp
_READY_STAGES = {
    SegmentState.WAITING_FOR_FILE_UPLOAD.value,
    SegmentState.WAITING_FOR_INSTI_TRADE.value,  # V6 Step-10 gate: "ready to trigger"
    SegmentState.WAITING_FOR_GTG.value,
}
# All others (INIT) get "checked_at"


def get_state(row: SegmentExecution, state_key: str) -> dict:
    """Return a copy of the state-level dict (empty dict if not yet written)."""
    return dict(row.processes_json.get(state_key, {}))


def set_state(row: SegmentExecution, state_key: str, state: dict) -> None:
    """
    Write a state-level dict into processes_json.
    Reassigns the entire column dict so SQLAlchemy detects the mutation.
    """
    updated = dict(row.processes_json)
    updated[state_key] = state
    row.processes_json = updated


def get_step(row: SegmentExecution, state_key: str, step_key: str) -> dict:
    """Return a copy of one step dict nested under a state (empty dict if not yet written)."""
    return dict(row.processes_json.get(state_key, {}).get("steps", {}).get(step_key, {}))


def set_step(row: SegmentExecution, state_key: str, step_key: str, step: dict) -> None:
    """Merge a step dict into processes_json[state_key]["steps"][step_key],
    preserving the state's other fields (e.g. "status")."""
    state = get_state(row, state_key)
    steps = dict(state.get("steps", {}))
    steps[step_key] = step
    state["steps"] = steps
    set_state(row, state_key, state)


def record_poll(row: SegmentExecution, state_key: str, step_key: str, last_response: str, now: datetime) -> None:
    """
    Record the latest CBOS response for a step that is still pending.
    Called on every file_process_status call that returns FALSE (still
    waiting). No "status" write here — current_state (on the row) is what
    drives control flow; this is purely a last_response/last_checked_at
    diagnostic log.
    """
    step = get_step(row, state_key, step_key)
    step["last_response"] = last_response
    step["last_checked_at"] = now.isoformat()
    set_step(row, state_key, step_key, step)


def mark_step_done(row: SegmentExecution, state_key: str, step_key: str, last_response: str, now: datetime) -> None:
    """
    Mark a step as complete (with the appropriate completion timestamp) and
    set the owning state's overall "status" to COMPLETED. Called when CBOS
    returns TRUE (or SKIP for INIT's/WAITING_FOR_GTG's holiday check).
    """
    state = get_state(row, state_key)
    steps = dict(state.get("steps", {}))
    step = dict(steps.get(step_key, {}))
    step["last_response"] = last_response

    if state_key in _CONFIRM_STAGES:
        step["confirmed_at"] = now.isoformat()
    elif state_key in _READY_STAGES:
        step["ready_at"] = now.isoformat()
    else:
        step["checked_at"] = now.isoformat()

    steps[step_key] = step
    state["steps"] = steps
    state["status"] = "COMPLETED"
    set_state(row, state_key, state)


def record_pid_reservation(
    row: SegmentExecution,
    process_id: str,
    source: str,
    now: datetime,
) -> None:
    """
    Record the PID-resolution outcome as a "reserve_process_id" step.
    (The step key predates the single-reserver contract - the engine now only
    READS the PID - and is kept verbatim for data compatibility with existing
    processes_json rows and their consumers; process_id_source is always
    "EXISTING" since commit 8e5e009.)

    Recorded as a "reserve_process_id" step
    nested inside WAITING_FOR_FILE_UPLOAD's own dict (its first-entry
    operation) — not a separate top-level key, so processes_json's
    top-level keys stay exactly the SegmentState vocabulary.
    """
    set_step(
        row,
        SegmentState.WAITING_FOR_FILE_UPLOAD.value,
        "reserve_process_id",
        {
            "process_id_reserved": process_id,
            "process_id_source": source,
            "reserved_at": now.isoformat(),
        },
    )


def get_pid_reservation(row: SegmentExecution) -> dict:
    """Return the "reserve_process_id" step recorded during WAITING_FOR_FILE_UPLOAD's first entry."""
    return get_step(row, SegmentState.WAITING_FOR_FILE_UPLOAD.value, "reserve_process_id")


def record_trigger_attempt(row: SegmentExecution, now: datetime) -> None:
    """
    Pre-commit write — double-trigger protection (see
    state_machine.RealSegmentStateMachine.handle_triggered).
    Durably records intent BEFORE the CBOS call, so a crash before the
    outcome is recorded leaves "TRIGGERING" for the recovery check to see.
    Carries process_id_source forward from WAITING_FOR_FILE_UPLOAD's
    "reserve_process_id" step into TRIGGERED's own dict, so it survives the
    eventual TRIGGERED/FAILED write.
    """
    pid_reservation = get_pid_reservation(row)
    set_state(
        row,
        SegmentState.TRIGGERED.value,
        {
            "status": "TRIGGERING",
            "attempt_started_at": now.isoformat(),
            "process_id_source": pid_reservation.get("process_id_source"),
        },
    )


def record_trigger(
    row: SegmentExecution,
    process_id: str,
    is_runnable: bool,
    now: datetime,
) -> None:
    """Record a successful trigger call, preserving process_id_source
    ("EXISTING") and attempt_started_at, both carried
    forward from record_trigger_attempt() — otherwise the moment a trigger
    confirms, the timestamp of when the attempt actually started (and thus
    how long CBOS took to confirm it) would be silently lost."""
    existing = get_state(row, SegmentState.TRIGGERED.value)
    set_state(
        row,
        SegmentState.TRIGGERED.value,
        {
            "status": "TRIGGERED",
            "attempt_started_at": existing.get("attempt_started_at"),
            "at": now.isoformat(),
            "process_id_used": process_id,
            "process_id_source": existing.get("process_id_source"),
            "is_runnable": is_runnable,
        },
    )


def record_trigger_failed(row: SegmentExecution, error: str, now: datetime) -> None:
    """
    Record a CONFIRMED, permanent trigger failure — only for non-transient
    failures, always paired with AbstractSegmentStateMachine._fail_result().
    Transient failures deliberately skip this, leaving "TRIGGERING" for recovery.
    """
    existing = get_state(row, SegmentState.TRIGGERED.value)
    set_state(
        row,
        SegmentState.TRIGGERED.value,
        {
            "status": "FAILED",
            "attempt_started_at": existing.get("attempt_started_at"),
            "at": now.isoformat(),
            "error": error,
            "process_id_source": existing.get("process_id_source"),
        },
    )


def record_post_trade_trigger_attempt(row: SegmentExecution, now: datetime) -> None:
    """
    Pre-commit crash-safety marker for post-trade triggers. Unlike the
    real-segment TRIGGERED step, there's no CBOS-side check afterwards, so
    this can't power an automatic re-trigger decision — it exists purely
    so a crash mid-call is durably visible; handle_triggered() refuses to
    re-fire when it sees this and requires manual verification instead.
    """
    set_state(
        row,
        SegmentState.TRIGGERED.value,
        {
            "status": "TRIGGERING",
            "attempt_started_at": now.isoformat(),
        },
    )


def record_post_trade_trigger(row: SegmentExecution, message: str, now: datetime) -> None:
    """Record a successful post-trade trigger call (no process_id involved),
    preserving attempt_started_at carried forward from
    record_post_trade_trigger_attempt() — same rationale as record_trigger()."""
    existing = get_state(row, SegmentState.TRIGGERED.value)
    set_state(
        row,
        SegmentState.TRIGGERED.value,
        {
            "status": "TRIGGERED",
            "attempt_started_at": existing.get("attempt_started_at"),
            "at": now.isoformat(),
            "message": message,
        },
    )


def record_post_trade_trigger_failed(row: SegmentExecution, error: str, now: datetime) -> None:
    """Record a failed post-trade trigger attempt, preserving
    attempt_started_at — same rationale as record_trigger_failed()."""
    existing = get_state(row, SegmentState.TRIGGERED.value)
    set_state(
        row,
        SegmentState.TRIGGERED.value,
        {
            "status": "FAILED",
            "attempt_started_at": existing.get("attempt_started_at"),
            "at": now.isoformat(),
            "error": error,
        },
    )


def record_download_result(
    row: SegmentExecution,
    manifest_path: str,
    batch_id: str,
    status: str,
    now: datetime,
) -> None:
    """DOWNLOADING succeeded — persist what the bot handed back. UPLOADING
    reads manifest_path from here; WAITING_FOR_FILE_UPLOAD's incomplete-batch
    check reads batch_id. Nested under the DOWNLOADING state key like every
    other per-state fact."""
    state = get_state(row, "DOWNLOADING")
    state["status"] = "COMPLETED"
    state["manifest_path"] = manifest_path
    state["batch_id"] = batch_id
    state["download_status"] = status
    state["downloaded_at"] = now.isoformat()
    set_state(row, "DOWNLOADING", state)


def get_download_result(row: SegmentExecution) -> dict:
    """The DOWNLOADING state dict ({} before any download completed)."""
    return get_state(row, "DOWNLOADING")
