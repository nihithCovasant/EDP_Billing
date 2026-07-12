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
    That's the full-detail API response; sending it straight to the email
    service would leak every internal/audit field as extra table columns
    (email service's DEFAULT_SEGMENT_COLUMNS only fixes column *order*, not
    which ones are included — see table_renderer.derive_columns()).

    Dropped vs. serialize_segment(): id/config_id_used (no ops value),
    created_at/updated_at (updated_at duplicates completed_at at alert time),
    process_id_reserved_at/last_heartbeat_at/runtime_health (live-status-only,
    meaningless once terminal), processes_json (too raw for email, available
    via GET /edp/status/...), skip_category/sequence_order (already filtered
    from the rendered table by table_renderer._LOW_SIGNAL_KEYS).

    Timestamps use _dt_ist() (short, no microseconds/offset — agent only
    ever runs in IST) instead of _dt()'s full ISO 8601.
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
