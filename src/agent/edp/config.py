"""
EDP agent bootstrap configuration loaded from agent_config.json.

This config covers agent-level settings (DB URL, CBOS URL, etc.).
Wake interval is read from EDP_WAKE_INTERVAL_SECONDS env var only.
Segment schedules and process definitions are stored in the edpb_properties DB
table and uploaded daily by ops.

On first run with no edpb_properties row, default_segments is used to
auto-seed a workflow for today so the agent can start without a manual upload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

from src.config.agent_config import load_agent_config
from .utils.constants import POST_TRADE_ORDER, POST_TRADE_FIRST_WINDOW_START
from cams_otel_lib import Logger as logger, otel_trace


def _normalize_postgres_url(url: str) -> str:
    """Ensure async SQLAlchemy driver prefix for PostgreSQL URLs."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _redact_target(url: str) -> str:
    """host:port/dbname only — never the password — for safe logging."""
    try:
        parsed = urlparse(url)
        return f"{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
    except Exception:
        return "<unparseable>"


def _resolve_database_url(edp_raw: Dict[str, Any], secrets: Dict[str, Any]) -> str:
    """
    Resolve the EDP (PostgreSQL-only) database URL. Priority (highest first):
      1. agent_config.json → secrets.database.postgres.connection_string
      2. agent_config.json → edp.database_url
      3. DATABASE_URL env var (full connection string) — fallback only
      4. DB_HOST / DB_PORT / DB_NAME / DB_USERNAME / DB_PASSWORD env vars — fallback only

    agent_config.json wins over env vars (not the other way around): this agent
    runs as a pod on the CAMS platform, where every pod shares a handful of
    ambient environment variables injected platform-wide — including a
    DATABASE_URL that actually points at a different service's Postgres (the
    LiteLLM proxy's own metadata DB). That ambient var is present in EVERY pod
    regardless of what this specific agent needs, so it can never be trusted as
    an intentional per-agent override — agent_config.json's explicit, per-agent
    config always wins. DATABASE_URL/DB_HOST remain a fallback for the case
    where agent_config.json has no database section configured at all (e.g. a
    fresh scaffold).

    No fallback at all if none of these are set — running against a throwaway
    on-disk database instead of the real Postgres instance is exactly the kind
    of silent misconfiguration this should never allow; raises immediately.
    """
    db_url = (
        secrets.get("database", {})
        .get("postgres", {})
        .get("connection_string")
    )
    if not db_url:
        db_url = edp_raw.get("database_url")

    if db_url:
        resolved = _normalize_postgres_url(db_url)
        _validate_database_url(resolved)

        # Diagnostic only — not a warning: an ambient platform env var being
        # correctly ignored is expected, healthy behavior on CAMS.
        ambient_env_url = os.getenv("DATABASE_URL", "").strip()
        ambient_db_host = os.getenv("DB_HOST", "").strip()
        if ambient_env_url or ambient_db_host:
            ambient_target = (
                _redact_target(_normalize_postgres_url(ambient_env_url))
                if ambient_env_url
                else f"{ambient_db_host}:{os.getenv('DB_PORT', '5432').strip()}/{os.getenv('DB_NAME', 'postgres').strip()}"
            )
            logger.info(
                f"[EDP CONFIG] Using agent_config.json's configured database "
                f"({_redact_target(resolved)}); ignoring ambient DATABASE_URL/DB_HOST "
                f"env var (points at {ambient_target}) — agent_config.json takes priority."
            )

        return resolved

    env_url = os.getenv("DATABASE_URL", "").strip()
    if env_url:
        resolved = _normalize_postgres_url(env_url)
        _validate_database_url(resolved)
        return resolved

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

    raise RuntimeError(
        "EDP database is not configured — set agent_config.json's "
        "secrets.database.postgres.connection_string / edp.database_url, "
        "or DATABASE_URL, or DB_HOST (+ DB_PORT/DB_NAME/DB_USERNAME/DB_PASSWORD) "
        "as a fallback. There is no local-file fallback: refusing to start "
        "against a throwaway database instead of the real PostgreSQL instance."
    )


def _env_nonempty(name: str) -> Optional[str]:
    """Like os.getenv(name), but treats an explicitly-set-to-empty-string
    env var (e.g. a copy-paste-gone-wrong deploy leaving CBOS_STATUS_URL=""
    in place) the same as an unset one, instead of letting "" silently win
    over the next fallback with zero warning — the "is this var present at
    all" check elsewhere in this module (e.g. cbos_status_url_defaulted)
    only catches a fully-absent key, not a present-but-blank one."""
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


_VALID_DATABASE_URL_PREFIXES = ("postgresql://", "postgresql+asyncpg://", "postgresql+psycopg://")


def _validate_database_url(url: str) -> None:
    """Fail fast, at config-load time, on an obviously-wrong DATABASE_URL
    shape (e.g. a copy-pasted mysql://, sqlite://, or plain hostname) —
    rather than letting it silently pass through to asyncpg's connection
    attempt, where the resulting error is several layers removed from the
    actual misconfiguration."""
    if not url.startswith(_VALID_DATABASE_URL_PREFIXES):
        raise RuntimeError(
            f"EDP database URL does not look like a PostgreSQL connection "
            f"string (got a URL starting with {url.split('://', 1)[0] if '://' in url else url!r}) — "
            f"expected one of {_VALID_DATABASE_URL_PREFIXES}. This agent is "
            f"PostgreSQL-only; check DATABASE_URL / DB_HOST / agent_config.json."
        )


def _validate_process_entries(entries: List[Dict[str, Any]], *, config_key: str, code_field: str) -> None:
    """Fail fast on a malformed default_segments/default_post_trade_processes
    entry (e.g. a bare string instead of a {"segment_code": ...} object) —
    otherwise this only surfaces later, at first-run auto-seed time, as an
    AttributeError from build_default_workflow_json()'s seg.get(...) call,
    with a traceback that doesn't point back to agent_config.json at all."""
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"agent_config.json's edp.{config_key}[{idx}] is not an object "
                f"(got {type(entry).__name__}: {entry!r}) — expected a dict with "
                f"at least a {code_field!r} key"
            )
        if not entry.get(code_field):
            raise RuntimeError(
                f"agent_config.json's edp.{config_key}[{idx}] is missing a "
                f"non-empty {code_field!r} value: {entry!r}"
            )


def to_alembic_url(database_url: str) -> str:
    """
    Convert the app's async SQLAlchemy URL to a sync URL for Alembic migrations.
    Alembic runs synchronously; the runtime app keeps using asyncpg.
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
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

    # LOGINID for the 5 T+1 post-trade trigger calls, distinct from cbos_login_id.
    post_trade_login_id: str = "G_LID"

    # Database (PostgreSQL only — always resolved explicitly by load_edp_config(),
    # this default is never actually used at runtime).
    database_url: str = ""

    # Identifies this agent instance in logs
    agent_instance_id: str = "agent-1"

    # Auto-seed defaults, used when no config has been uploaded yet.
    default_segments: List[Dict[str, Any]] = field(default_factory=list)
    # Empty means "use fixed legacy defaults" (see build_default_workflow_json()).
    default_post_trade_processes: List[Dict[str, Any]] = field(default_factory=list)


def _is_truthy_env(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in ("1", "true", "yes", "on")


@otel_trace
def load_edp_config() -> EdpBootstrapConfig:
    """
    Load EDP bootstrap settings from agent_config.json under agent_config.edp.

    Logs a WARNING for any critical setting (CBOS URLs/mock flag, database
    URL) that fell through to a hardcoded default rather than an explicit
    env var / config value. If EDP_STRICT_CONFIG=true, raises instead of
    starting up silently misconfigured (opt-in, off by default).
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

    # Env vars take priority over agent_config.json, then hardcoded defaults.
    # Unlike the settings below, an unresolvable database_url is not a "soft"
    # default to warn about — _resolve_database_url() raises immediately,
    # unconditionally, since there is no safe fallback to run against.
    secrets = default_cfg.get("secrets", {})
    db_url = _resolve_database_url(edp_raw, secrets)

    cbos_status_url = (
        _env_nonempty("CBOS_STATUS_URL")
        or edp_raw.get("cbos_status_url")
        or "http://localhost:8087"
    )
    cbos_status_url_defaulted = not _env_nonempty("CBOS_STATUS_URL") and not edp_raw.get("cbos_status_url")

    cbos_process_url = (
        _env_nonempty("CBOS_PROCESS_URL")
        or edp_raw.get("cbos_process_url")
        or "http://localhost:8003"
    )
    cbos_process_url_defaulted = not _env_nonempty("CBOS_PROCESS_URL") and not edp_raw.get("cbos_process_url")

    cbos_use_mock_raw = os.getenv("CBOS_USE_MOCK")
    cbos_use_mock_defaulted = cbos_use_mock_raw is None and "cbos_use_mock" not in edp_raw
    if cbos_use_mock_raw is not None:
        cbos_use_mock = _is_truthy_env(cbos_use_mock_raw)
    else:
        cbos_use_mock = bool(edp_raw.get("cbos_use_mock", True))

    defaulted_settings = [
        name for name, defaulted in (
            ("cbos_status_url", cbos_status_url_defaulted),
            ("cbos_process_url", cbos_process_url_defaulted),
            ("cbos_use_mock", cbos_use_mock_defaulted),
        ) if defaulted
    ]
    if defaulted_settings:
        logger.warning(
            f"[EDP CONFIG] Settings falling through to hardcoded defaults (no env var, "
            f"no agent_config.json value): {defaulted_settings} — resolved "
            f"cbos_use_mock={cbos_use_mock}"
        )
        if _is_truthy_env(os.getenv("EDP_STRICT_CONFIG")):
            raise RuntimeError(
                "EDP_STRICT_CONFIG=true: refusing to start with unconfigured EDP settings "
                f"{defaulted_settings} — set the corresponding env var(s) or "
                "agent_config.json 'edp' fields explicitly"
            )

    wake_interval_seconds = int(os.getenv("EDP_WAKE_INTERVAL_SECONDS", "60"))
    if wake_interval_seconds <= 0:
        raise RuntimeError(
            f"EDP_WAKE_INTERVAL_SECONDS must be a positive integer, got "
            f"{wake_interval_seconds} — 0 or negative would busy-loop the wake cycle "
            f"(asyncio.sleep(<=0) returns immediately) rather than pacing it."
        )

    active_date_cutoff_hour = int(edp_raw.get("active_date_cutoff_hour", 6))
    if not (0 <= active_date_cutoff_hour <= 23):
        raise RuntimeError(
            f"agent_config.json's edp.active_date_cutoff_hour must be 0-23 (an hour "
            f"of the day), got {active_date_cutoff_hour}. Note: 0 is a real but easily "
            f"mistaken value — it does NOT mean 'no rollover happens at midnight', it "
            f"means the trade-date rollover never happens at all, which can "
            f"misattribute billing to the wrong trade date."
        )

    default_segments = edp_raw.get("segments", [])
    default_post_trade_processes = edp_raw.get("post_trade_processes", [])
    _validate_process_entries(default_segments, config_key="segments", code_field="segment_code")
    _validate_process_entries(
        default_post_trade_processes, config_key="post_trade_processes", code_field="process_code",
    )

    return EdpBootstrapConfig(
        wake_interval_seconds=wake_interval_seconds,
        active_date_cutoff_hour=active_date_cutoff_hour,
        timezone=edp_raw.get("timezone", "Asia/Kolkata"),
        cbos_status_url=cbos_status_url,
        cbos_process_url=cbos_process_url,
        cbos_use_mock=cbos_use_mock,
        cbos_login_id=os.getenv("CBOS_LOGIN_ID", edp_raw.get("cbos_login_id", "CV0001")),
        post_trade_login_id=os.getenv(
            "POST_TRADE_LOGIN_ID", edp_raw.get("post_trade_login_id", "G_LID")
        ),
        database_url=db_url,
        agent_instance_id=edp_raw.get("agent_instance_id", "agent-1"),
        default_segments=default_segments,
        default_post_trade_processes=default_post_trade_processes,
    )


def build_default_workflow_json(
    segments: List[Dict[str, Any]],
    post_trade_processes: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """
    Build a workflow_json from default_segments / default_post_trade_processes.

    No "timezone" field — the agent only ever operates in IST (see
    EdpBootstrapConfig.timezone). Pipeline stages, processing order, and
    display names are fixed code constants, not part of the config — this
    only carries segment/process identity + timing metadata. Passing
    post_trade_processes=None builds the fixed legacy defaults.

    No window_end_next_day field either — orchestrator._resolve_window()
    derives the rollover itself from window_start/window_end (next day
    only if window_end is at/before window_start), not something a config
    states.

    No wake_interval_seconds field either — that's an agent-level runtime
    setting (EDP_WAKE_INTERVAL_SECONDS env var / EdpBootstrapConfig), not a
    per-day config an ops uploader should be able to override.
    """
    built_segments = []
    for seg in segments:
        seg_code = seg.get("segment_code", "")
        built_segments.append({
            "segment_code": seg_code,
            "login_id": seg.get("login_id", "CV0001"),
            "window_start": seg.get("window_start", "17:00"),
            "window_end": seg.get("window_end", "06:00"),
        })

    if post_trade_processes is None:
        post_trade_processes = [{"process_code": code} for code in POST_TRADE_ORDER]
        post_trade_processes[0] = {
            **post_trade_processes[0],
            "window_start": POST_TRADE_FIRST_WINDOW_START,
        }

    built_post_trade = []
    for proc in post_trade_processes:
        entry: Dict[str, Any] = {
            "process_code": proc.get("process_code", ""),
            "login_id": proc.get("login_id", "G_LID"),
        }
        if proc.get("gtg_process_name"):
            entry["gtg_process_name"] = proc["gtg_process_name"]
        if proc.get("window_start"):
            entry["window_start"] = proc["window_start"]
        if proc.get("window_end"):
            entry["window_end"] = proc["window_end"]
        built_post_trade.append(entry)

    return {
        "segments": built_segments,
        "post_trade_processes": built_post_trade,
    }
