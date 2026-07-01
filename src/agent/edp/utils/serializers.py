"""Serialization helpers — convert SQLAlchemy rows to API-safe dicts."""

from __future__ import annotations

from ..models import SegmentExecution


def serialize_segment(row: SegmentExecution) -> dict:
    """Full detail — used by GET /edp/status/{date}/{segment_code}."""
    return {
        "id": row.id,
        "trade_date": row.trade_date.isoformat(),
        "domain": row.domain,
        "segment_code": row.segment_code,
        "segment_name": row.segment_name,
        "sequence_order": row.sequence_order,
        "segment_status": row.segment_status.value,
        "current_process": row.current_process,
        "current_phase": row.current_phase.value if row.current_phase else None,
        "process_id": row.process_id,
        "process_id_reserved_at": _dt(row.process_id_reserved_at),
        "skip_category": row.skip_category,
        "skip_reason": row.skip_reason,
        "window_start_at": _dt(row.window_start_at),
        "window_end_at": _dt(row.window_end_at),
        "started_at": _dt(row.started_at),
        "completed_at": _dt(row.completed_at),
        "last_heartbeat_at": _dt(row.last_heartbeat_at),
        "runtime_health": row.runtime_health.value,
        "lock_state": row.lock_state.value,
        "lock_owner": row.lock_owner,
        "config_id_used": row.config_id_used,
        "config_hash_used": row.config_hash_used,
        "processes_json": row.processes_json or {},
        "hitl_json": row.hitl_json or [],
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def serialize_segment_summary(row: SegmentExecution) -> dict:
    """Compact summary — used inside GET /edp/status/{date} day view."""
    return {
        "segment_code": row.segment_code,
        "segment_name": row.segment_name,
        "sequence_order": row.sequence_order,
        "segment_status": row.segment_status.value,
        "current_process": row.current_process,
        "current_phase": row.current_phase.value if row.current_phase else None,
        "process_id": row.process_id,
        "process_id_reserved_at": _dt(row.process_id_reserved_at),
        "skip_category": row.skip_category,
        "skip_reason": row.skip_reason,
        "window_start_at": _dt(row.window_start_at),
        "window_end_at": _dt(row.window_end_at),
        "started_at": _dt(row.started_at),
        "completed_at": _dt(row.completed_at),
        "last_heartbeat_at": _dt(row.last_heartbeat_at),
        "runtime_health": row.runtime_health.value,
        "processes_json": row.processes_json or {},
    }


def _dt(value) -> str | None:
    return value.isoformat() if value else None
