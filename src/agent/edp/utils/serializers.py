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
        "current_phase": row.current_phase.value if row.current_phase else None,
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


def serialize_segment_summary(row: SegmentExecution) -> dict:
    """Compact summary — used inside GET /edp/status/{date} day view."""
    return {
        "segment_code": row.segment_code,
        "segment_name": get_segment_name(row.segment_code),
        "sequence_order": get_sequence_order(row.segment_code),
        "segment_status": row.segment_status.value,
        "current_process": row.current_process,
        "current_phase": row.current_phase.value if row.current_phase else None,
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
