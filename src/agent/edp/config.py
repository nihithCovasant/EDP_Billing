"""
EDP agent bootstrap configuration loaded from agent_config.json.

This config covers only agent-level settings (DB URL, CBOS URL, wake interval).
Segment schedules and process definitions are stored in workflow_properties DB table
and uploaded daily by ops.

On first run with no workflow_properties row, default_segments is used to
auto-seed a workflow for today so the agent can start without a manual upload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from src.config.agent_config import load_agent_config
from cams_otel_lib import Logger as logger, otel_trace


@dataclass
class EdpBootstrapConfig:
    # Loop settings
    wake_interval_seconds: int = 60
    active_date_cutoff_hour: int = 6   # before 06:00 IST = previous calendar day
    timezone: str = "Asia/Kolkata"

    # CBOS — two separate base URLs
    cbos_status_url: str = "http://localhost:8087"    # port 8087: file_process_status
    cbos_process_url: str = "http://localhost:8003"   # port 8003: getNewTradeProcess etc.
    cbos_use_mock: bool = True
    cbos_login_id: str = "CV0001"

    # Database
    database_url: str = "sqlite+aiosqlite:///./edp_agent.db"

    # Lock TTL for double-trigger prevention
    lock_ttl_seconds: int = 300

    # Unique ID for this agent pod (used as lock_owner)
    agent_instance_id: str = "agent-1"

    # Default segment definitions — used to auto-seed workflow_properties
    # when no config has been uploaded for today yet.
    default_segments: List[Dict[str, Any]] = field(default_factory=list)


@otel_trace
def load_edp_config() -> EdpBootstrapConfig:
    """Load EDP bootstrap settings from agent_config.json under agent_config.edp."""
    raw = load_agent_config()
    default_cfg = raw.get("default", {})
    edp_raw: Dict[str, Any] = default_cfg.get("edp", {})

    # Database URL — prefer secrets.database.postgres, fall back to edp.database_url
    secrets = default_cfg.get("secrets", {})
    db_url = (
        secrets.get("database", {})
        .get("postgres", {})
        .get("connection_string")
    )
    if db_url and db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not db_url:
        db_url = edp_raw.get("database_url", "sqlite+aiosqlite:///./edp_agent.db")

    return EdpBootstrapConfig(
        wake_interval_seconds=int(edp_raw.get("wake_interval_seconds", 60)),
        active_date_cutoff_hour=int(edp_raw.get("active_date_cutoff_hour", 6)),
        timezone=edp_raw.get("timezone", "Asia/Kolkata"),
        cbos_status_url=edp_raw.get("cbos_status_url", "http://localhost:8087"),
        cbos_process_url=edp_raw.get("cbos_process_url", "http://localhost:8003"),
        cbos_use_mock=bool(edp_raw.get("cbos_use_mock", True)),
        cbos_login_id=edp_raw.get("cbos_login_id", "CV0001"),
        database_url=db_url,
        lock_ttl_seconds=int(edp_raw.get("lock_ttl_seconds", 300)),
        agent_instance_id=edp_raw.get("agent_instance_id", "agent-1"),
        default_segments=edp_raw.get("segments", []),
    )


def build_default_workflow_json(
    segments: List[Dict[str, Any]],
    timezone: str = "Asia/Kolkata",
    domain: str = "EDP",
) -> dict:
    """
    Build a workflow_json from the default_segments list in bootstrap config.
    The 7-stage pipeline (BeginFileUpload → RESERVE_PID → FILEUPLOAD → TRIGGER
    → BILLPOSTING → RECON → CONTRACTNOTEGENERATION) is fixed in the orchestrator
    and does not need to be listed per segment in the workflow_json.
    The workflow_json only carries segment identity + timing metadata.
    """
    built_segments = []
    for seg in segments:
        seg_code = seg.get("segment_code", "")
        built_segments.append({
            "segment_code": seg_code,
            "segment_name": seg.get("segment_name", seg_code),
            "sequence_order": seg.get("sequence_order", 99),
            "login_id": seg.get("login_id", "CV0001"),
            "window_start": seg.get("window_start", "17:00"),
            "window_end": seg.get("window_end", "06:00"),
            "window_end_next_day": seg.get("window_end_next_day", True),
        })

    return {
        "domain": domain,
        "timezone": timezone,
        "wake_interval_seconds": 60,
        "segments": built_segments,
    }
