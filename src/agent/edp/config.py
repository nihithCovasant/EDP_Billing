"""
EDP agent bootstrap configuration loaded from agent_config.json.

This config covers only agent-level settings (DB URL, CBOS URL, wake interval).
Segment schedules and process definitions are stored in the edp_properties DB
table and uploaded daily by ops.

On first run with no edp_properties row, default_segments is used to
auto-seed a workflow for today so the agent can start without a manual upload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List
from urllib.parse import quote_plus

from src.config.agent_config import load_agent_config
from cams_otel_lib import Logger as logger, otel_trace


def _normalize_postgres_url(url: str) -> str:
    """Ensure async SQLAlchemy driver prefix for PostgreSQL URLs."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _resolve_database_url(edp_raw: Dict[str, Any], secrets: Dict[str, Any]) -> str:
    """
    Resolve the EDP database URL. Priority (highest first):
      1. DATABASE_URL env var (full connection string)
      2. DB_HOST / DB_PORT / DB_NAME / DB_USERNAME / DB_PASSWORD env vars
      3. agent_config.json → secrets.database.postgres.connection_string
      4. agent_config.json → edp.database_url
      5. SQLite fallback (local dev)
    """
    env_url = os.getenv("DATABASE_URL", "").strip()
    if env_url:
        return _normalize_postgres_url(env_url)

    db_host = os.getenv("DB_HOST", "").strip()
    if db_host:
        db_port = os.getenv("DB_PORT", "5432").strip()
        db_name = os.getenv("DB_NAME", "postgres").strip()
        db_user = os.getenv("DB_USERNAME", "postgres").strip()
        db_password = os.getenv("DB_PASSWORD", "")
        userinfo = quote_plus(db_user)
        if db_password:
            userinfo = f"{userinfo}:{quote_plus(db_password)}"
        return f"postgresql+asyncpg://{userinfo}@{db_host}:{db_port}/{db_name}"

    db_url = (
        secrets.get("database", {})
        .get("postgres", {})
        .get("connection_string")
    )
    if db_url:
        return _normalize_postgres_url(db_url)

    return edp_raw.get("database_url", "sqlite+aiosqlite:///./edp_agent.db")


def to_alembic_url(database_url: str) -> str:
    """
    Convert the app's async SQLAlchemy URL to a sync URL for Alembic migrations.
    Alembic runs synchronously; the runtime app keeps using asyncpg / aiosqlite.
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("sqlite+aiosqlite:"):
        return database_url.replace("sqlite+aiosqlite:", "sqlite:", 1)
    return database_url


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

    # LOGINID used for the 5 T+1 post-trade trigger calls (GetCollateralValuation,
    # MTFTradeProcessCollateralAllocation/FundTransfer, DailyMarginReporting/
    # Statements) — a distinct login ID from cbos_login_id, per spec ("G_LID").
    post_trade_login_id: str = "G_LID"

    # Database
    database_url: str = "sqlite+aiosqlite:///./edp_agent.db"

    # Lock TTL for double-trigger prevention
    lock_ttl_seconds: int = 300

    # Unique ID for this agent pod (used as lock_owner)
    agent_instance_id: str = "agent-1"

    # Default segment definitions — used to auto-seed edp_properties
    # when no config has been uploaded for today yet.
    default_segments: List[Dict[str, Any]] = field(default_factory=list)


def _is_truthy_env(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in ("1", "true", "yes", "on")


@otel_trace
def load_edp_config() -> EdpBootstrapConfig:
    """
    Load EDP bootstrap settings from agent_config.json under agent_config.edp.

    A missing/empty/malformed `edp` config section used to fail silently:
    every field just fell through to a hardcoded default — cbos_use_mock=
    True pointing at localhost, SQLite for the database — with nothing in
    the logs to say so. A broken production deploy would start up
    "healthy," run entirely in mock mode against no real CBOS all day, and
    report success. This function now:
      1. Always logs, at WARNING level, exactly which critical settings
         (CBOS URLs/mock flag, database URL) fell through to a hardcoded
         default rather than an explicit env var or agent_config.json value
         — impossible to miss in production logs / log-based alerting.
      2. Raises RuntimeError instead of starting up silently misconfigured
         when EDP_STRICT_CONFIG=true is set (intended for production
         deployment manifests) and ANY such setting fell through to a
         hardcoded default. Left opt-in (not the default) so local dev /
         zero-config test runs are unaffected.
    """
    raw = load_agent_config()
    default_cfg = raw.get("default", {})
    edp_raw: Dict[str, Any] = default_cfg.get("edp", {})
    if not isinstance(edp_raw, dict):
        logger.error(
            f"[EDP CONFIG] agent_config.json's default.edp section is not an object "
            f"(got {type(edp_raw).__name__}) — ignoring it entirely, every EDP setting "
            f"will fall through to env vars / hardcoded defaults"
        )
        edp_raw = {}
    elif not edp_raw:
        logger.warning(
            "[EDP CONFIG] agent_config.json has no default.edp section (or it's empty) — "
            "every EDP setting will rely on env vars / hardcoded defaults; this is normal "
            "for a fully env-var-driven deployment, but verify that's intentional"
        )

    # Database URL — env vars (DATABASE_URL or DB_*) take priority; see _resolve_database_url
    secrets = default_cfg.get("secrets", {})
    db_url = _resolve_database_url(edp_raw, secrets)
    db_url_defaulted = db_url == "sqlite+aiosqlite:///./edp_agent.db" and not edp_raw.get("database_url")

    # CBOS URLs / mock toggle — env vars take priority over agent_config.json
    # so switching between the Mock CBOS Server, the real CBOS system, or
    # in-process mock responses is a single-place .env edit (see
    # mock_cbos/README.md). Falls back to agent_config.json, then hardcoded
    # defaults, if the env vars are not set.
    # Preserves the exact original resolution semantics — os.getenv's own
    # default argument only kicks in when the env var is completely UNSET,
    # not when it's set to an empty string — while still being able to
    # report (for logging only) whether the value ended up as the
    # hardcoded fallback with nothing explicitly configured anywhere.
    cbos_status_url = os.getenv(
        "CBOS_STATUS_URL", edp_raw.get("cbos_status_url", "http://localhost:8087")
    )
    cbos_status_url_defaulted = "CBOS_STATUS_URL" not in os.environ and not edp_raw.get("cbos_status_url")

    cbos_process_url = os.getenv(
        "CBOS_PROCESS_URL", edp_raw.get("cbos_process_url", "http://localhost:8003")
    )
    cbos_process_url_defaulted = "CBOS_PROCESS_URL" not in os.environ and not edp_raw.get("cbos_process_url")

    cbos_use_mock_raw = os.getenv("CBOS_USE_MOCK")
    cbos_use_mock_defaulted = cbos_use_mock_raw is None and "cbos_use_mock" not in edp_raw
    if cbos_use_mock_raw is not None:
        cbos_use_mock = _is_truthy_env(cbos_use_mock_raw)
    else:
        cbos_use_mock = bool(edp_raw.get("cbos_use_mock", True))

    defaulted_settings = [
        name for name, defaulted in (
            ("database_url", db_url_defaulted),
            ("cbos_status_url", cbos_status_url_defaulted),
            ("cbos_process_url", cbos_process_url_defaulted),
            ("cbos_use_mock", cbos_use_mock_defaulted),
        ) if defaulted
    ]
    if defaulted_settings:
        logger.warning(
            f"[EDP CONFIG] Settings falling through to hardcoded defaults (no env var, "
            f"no agent_config.json value): {defaulted_settings} — resolved "
            f"cbos_use_mock={cbos_use_mock} database_url={db_url!r}"
        )
        if _is_truthy_env(os.getenv("EDP_STRICT_CONFIG")):
            raise RuntimeError(
                "EDP_STRICT_CONFIG=true: refusing to start with unconfigured EDP settings "
                f"{defaulted_settings} — set the corresponding env var(s) or "
                "agent_config.json 'edp' fields explicitly"
            )

    return EdpBootstrapConfig(
        wake_interval_seconds=int(edp_raw.get("wake_interval_seconds", 60)),
        active_date_cutoff_hour=int(edp_raw.get("active_date_cutoff_hour", 6)),
        timezone=edp_raw.get("timezone", "Asia/Kolkata"),
        cbos_status_url=cbos_status_url,
        cbos_process_url=cbos_process_url,
        cbos_use_mock=cbos_use_mock,
        cbos_login_id=os.getenv("CBOS_LOGIN_ID", edp_raw.get("cbos_login_id", "CV0001")),
        post_trade_login_id=os.getenv(
            "POST_TRADE_LOGIN_ID", edp_raw.get("post_trade_login_id", "G_LID")
        ),
        database_url=db_url,
        lock_ttl_seconds=int(edp_raw.get("lock_ttl_seconds", 300)),
        agent_instance_id=edp_raw.get("agent_instance_id", "agent-1"),
        default_segments=edp_raw.get("segments", []),
    )


def build_default_workflow_json(
    segments: List[Dict[str, Any]],
    timezone: str = "Asia/Kolkata",
) -> dict:
    """
    Build a workflow_json from the default_segments list in bootstrap config.
    The 7-stage pipeline (BeginFileUpload → RESERVE_PID → FILEUPLOAD → TRIGGER
    → BILLPOSTING → RECON → CONTRACTNOTEGENERATION) is fixed in the orchestrator
    and does not need to be listed per segment in the workflow_json.
    The workflow_json only carries segment identity + timing metadata.

    Processing order and display name are NOT included here — both are fixed
    code constants resolved from segment_code (see utils/constants.py), not
    part of the config. This system is also EDP-only, so there's no domain
    field to carry either.
    """
    built_segments = []
    for seg in segments:
        seg_code = seg.get("segment_code", "")
        built_segments.append({
            "segment_code": seg_code,
            "login_id": seg.get("login_id", "CV0001"),
            "window_start": seg.get("window_start", "17:00"),
            "window_end": seg.get("window_end", "06:00"),
            "window_end_next_day": seg.get("window_end_next_day", True),
        })

    return {
        "timezone": timezone,
        "wake_interval_seconds": 60,
        "segments": built_segments,
    }
