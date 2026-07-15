"""Service settings.

Configuration comes from the host project's agent_config.json (its
`agent_config.secrets.env` block), bridged into os.environ at import by
_apply_agent_config_env() below — so no .env file is required, matching the
rest of the project. Real process environment variables still override, and a
legacy .env is honored as a last-resort fallback if one happens to exist.

Email is delivered via Microsoft Graph `sendMail` (OAuth2 client-credentials),
not SMTP — see graph_client.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


def _apply_agent_config_env() -> None:
    """Bridge the host project's agent_config.json `agent_config.secrets.env`
    block into os.environ so this service is configured from that single
    source — no .env file required, matching the rest of the project.

    When this service runs *embedded* in the EDP agent, the agent has already
    applied that block, so these values are present and this is a cheap no-op.
    When run *standalone* (`python -m global_email_service.main`), this locates
    agent_config.json itself. Uses setdefault(), so a real, explicitly-set
    environment variable still overrides the config value. Best-effort: any
    failure leaves os.environ untouched.
    """
    try:
        candidates: List[Path] = []
        ext = os.getenv("APP_CONFIG_PATH")
        if ext:
            candidates.append(Path(ext))
        # Walk up from this file and from the cwd looking for src/config/agent_config.json,
        # so the bridge works regardless of where the process was launched from.
        for base in (Path(__file__).resolve(), Path.cwd().resolve()):
            for parent in [base, *base.parents]:
                candidates.append(parent / "src" / "config" / "agent_config.json")
        seen = set()
        for cfg_path in candidates:
            if cfg_path in seen:
                continue
            seen.add(cfg_path)
            if cfg_path.exists():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                env_block = data.get("agent_config", {}).get("secrets", {}).get("env", {})
                if isinstance(env_block, dict):
                    for key, value in env_block.items():
                        if value is not None:
                            os.environ.setdefault(str(key), str(value))
                return
    except Exception:
        pass


# Configure from agent_config.json first (single source of truth), then fall
# back to load_dotenv() for any legacy .env — a no-op when no .env is present.
# Both use setdefault semantics, so a real process env var always wins.
_apply_agent_config_env()

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _split_addresses(raw: str) -> List[str]:
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_first(*names: str, default: str = "") -> str:
    """First non-empty value among `names`, checked in order (for host/port aliasing)."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _env_or_file(name: str) -> Optional[str]:
    """`NAME` env var, or the contents of the file at `NAME_FILE` (stripped).

    Supports the common Kubernetes/Docker-secrets pattern of mounting a
    secret as a file rather than passing it in plaintext as an env var —
    useful when this service is deployed via CAMS with secrets injected as
    mounted files (e.g. `/run/secrets/...`).
    """
    value = os.getenv(name)
    if value:
        return value
    file_path = os.getenv(f"{name}_FILE")
    if file_path and Path(file_path).exists():
        return Path(file_path).read_text(encoding="utf-8").strip() or None
    return None


@dataclass
class EmailServiceConfig:
    # Microsoft Graph — OAuth2 client-credentials + sendMail as the sender mailbox.
    graph_tenant_id: Optional[str] = None
    graph_client_id: Optional[str] = None
    graph_client_secret: Optional[str] = None
    graph_sender: str = "rms@covasant.com"
    timeout_seconds: float = 15.0

    from_name: str = "EDP Billing Alerts"

    default_to: List[str] = field(default_factory=list)
    default_cc: List[str] = field(default_factory=list)

    max_retries: int = 2
    retry_backoff_seconds: float = 2.0
    dry_run: bool = False


def load_email_config() -> EmailServiceConfig:
    return EmailServiceConfig(
        graph_tenant_id=_env_or_file("EMAIL_GRAPH_TENANT_ID"),
        graph_client_id=_env_or_file("EMAIL_GRAPH_CLIENT_ID"),
        graph_client_secret=_env_or_file("EMAIL_GRAPH_CLIENT_SECRET"),
        graph_sender=os.getenv("EMAIL_GRAPH_SENDER", "rms@covasant.com"),
        timeout_seconds=float(os.getenv("EMAIL_GRAPH_TIMEOUT_SECONDS", "15")),
        from_name=os.getenv("EMAIL_FROM_NAME", "EDP Billing Alerts"),
        default_to=_split_addresses(os.getenv("EMAIL_DEFAULT_TO", "")),
        default_cc=_split_addresses(os.getenv("EMAIL_DEFAULT_CC", "")),
        max_retries=int(os.getenv("EMAIL_MAX_RETRIES", "2")),
        retry_backoff_seconds=float(os.getenv("EMAIL_RETRY_BACKOFF_SECONDS", "2")),
        dry_run=_env_bool("EMAIL_DRY_RUN", False),
    )


def load_server_settings() -> tuple[str, int, str]:
    """(host, port, log_level) for main.py's standalone server.

    Port prefers this service's dedicated EMAIL_SERVICE_PORT over the generic
    PORT: the project's single agent_config.json sets PORT for the *main* EDP
    agent (8005), so the email server must have its own port to avoid a
    collision. Host/log_level are safely shared (0.0.0.0 / INFO)."""
    host = _env_first("EMAIL_SERVICE_HOST", "HOST", default="0.0.0.0")
    port = int(_env_first("EMAIL_SERVICE_PORT", "PORT", default="9200"))
    log_level = _env_first("LOG_LEVEL", default="INFO")
    return host, port, log_level
