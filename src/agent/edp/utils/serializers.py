"""Serialization helpers — convert SQLAlchemy rows to API-safe dicts."""

from __future__ import annotations

from ..models import SegmentExecution, SegmentStatus
from .constants import STALE_HEARTBEAT_THRESHOLD, get_segment_name, get_sequence_order
from .datetime_utils import ensure_aware, now_ist


def _runtime_health(row: SegmentExecution) -> str:
    """Computed live, not persisted — a segment is STALE if it's IN_PROGRESS
    but hasn't had a heartbeat in a while."""
    if (
        row.segment_status == SegmentStatus.IN_PROGRESS
        and row.last_heartbeat_at
        and (now_ist() - ensure_aware(row.last_heartbeat_at)) > STALE_HEARTBEAT_THRESHOLD
    ):
        return "STALE"
    return "ACTIVE"


def serialize_segment(row: SegmentExecution) -> dict:
    """Full detail — used by GET /edp/status/{date}/{segment_code}."""
    return {
        "id": row.id,
        "trade_date": row.trade_date.isoformat(),
        "segment_code": row.segment_code,
        "segment_name": get_segment_name(row.segment_code),
        "sequence_order": get_sequence_order(row.segment_code),
        "segment_status": row.segment_status.value,
        "current_process": row.current_process,
        "current_state": row.current_state.value if row.current_state else None,
        "process_id": row.process_id,
        "process_id_reserved_at": _dt(row.process_id_reserved_at),
        "skip_category": row.skip_category,
        "skip_reason": row.skip_reason,
        "started_at": _dt(row.started_at),
        "completed_at": _dt(row.completed_at),
        "last_heartbeat_at": _dt(row.last_heartbeat_at),
        "runtime_health": _runtime_health(row),
        "config_id_used": row.config_id_used,
        "processes_json": row.processes_json or {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def serialize_segment_alert(row: SegmentExecution) -> dict:
    """
    Terminal-status email alert payload — deliberately NOT serialize_segment().
    That function is the full-detail API response (GET /edp/status/.../{code}),
    meant for debugging/audit; sending it straight to the email service leaks
    every internal/audit field (id, config_id_used, process_id_reserved_at,
    last_heartbeat_at, runtime_health, processes_json, created_at, updated_at)
    as extra table columns, since the email service's DEFAULT_SEGMENT_COLUMNS
    only fixes the *order* of known columns, not which ones get included —
    anything else on the row is appended as an "extra" (see
    global_email_service.table_renderer.derive_columns()).

    Dropped vs. serialize_segment(), and why:
      - id, config_id_used: internal identifiers, no ops value in an alert
        (process_id is what actually matters operationally).
      - created_at, updated_at: updated_at is always set to the exact same
        timestamp as completed_at in the same set_state() call that fires
        this alert — pure duplication. created_at (row-insert time) isn't
        actionable either.
      - process_id_reserved_at, last_heartbeat_at, runtime_health: diagnostic
        fields for the live status view, not meaningful once a segment has
        already reached a terminal state (which is the only time an alert
        fires).
      - processes_json: the full step-by-step audit trail — genuinely useful
        for debugging, but too raw (a flat "key: value; ..." blob) for an
        email; anyone needing it can pull it from GET /edp/status/....
      - skip_category, sequence_order: also dropped, but for a different
        reason — the email service's table_renderer._LOW_SIGNAL_KEYS
        already silently filters these out of the displayed columns, and
        neither feeds row coloring (colors.STATUS_LIKE_FIELDS keys off
        segment_status only). Including them would just be payload noise
        with zero effect on the rendered email.

    Timestamps use _dt_ist() (short, no microseconds, no +05:30 suffix —
    the agent only ever runs in IST, so the offset is implied, not
    informative) instead of _dt()'s full ISO 8601.
    """
    return {
        "trade_date": row.trade_date.isoformat(),
        "segment_code": row.segment_code,
        "segment_name": get_segment_name(row.segment_code),
        "segment_status": row.segment_status.value,
        "current_process": row.current_process,
        "current_state": row.current_state.value if row.current_state else None,
        "process_id": row.process_id,
        "skip_reason": row.skip_reason,
        "started_at": _dt_ist(row.started_at),
        "completed_at": _dt_ist(row.completed_at),
    }


def serialize_segment_summary(row: SegmentExecution) -> dict:
    """Compact summary — used inside GET /edp/status/{date} day view."""
    return {
        "segment_code": row.segment_code,
        "segment_name": get_segment_name(row.segment_code),
        "sequence_order": get_sequence_order(row.segment_code),
        "segment_status": row.segment_status.value,
        "current_process": row.current_process,
        "current_state": row.current_state.value if row.current_state else None,
        "process_id": row.process_id,
        "process_id_reserved_at": _dt(row.process_id_reserved_at),
        "skip_category": row.skip_category,
        "skip_reason": row.skip_reason,
        "started_at": _dt(row.started_at),
        "completed_at": _dt(row.completed_at),
        "last_heartbeat_at": _dt(row.last_heartbeat_at),
        "runtime_health": _runtime_health(row),
        "processes_json": row.processes_json or {},
    }


def _dt(value) -> str | None:
    return value.isoformat() if value else None


def _dt_ist(value) -> str | None:
    """Short display form for alert emails — 'YYYY-MM-DD HH:MM:SS IST'.
    No microseconds, no +05:30 offset: the agent only ever runs in IST
    (see EdpBootstrapConfig.timezone), so a numeric UTC offset repeated on
    every row is noise, not information."""
    if not value:
        return None
    return ensure_aware(value).strftime("%Y-%m-%d %H:%M:%S IST")
