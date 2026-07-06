"""Service settings loaded from src/global_email_service/.env.

Email is delivered via Microsoft Graph `sendMail` (OAuth2 client-credentials),
not SMTP — see graph_client.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


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
    """(host, port, log_level) for main.py — CAMS-style HOST/PORT/LOG_LEVEL env vars,
    falling back to this module's own EMAIL_SERVICE_* names for backward compatibility."""
    host = _env_first("HOST", "EMAIL_SERVICE_HOST", default="0.0.0.0")
    port = int(_env_first("PORT", "EMAIL_SERVICE_PORT", default="9200"))
    log_level = _env_first("LOG_LEVEL", default="INFO")
    return host, port, log_level
