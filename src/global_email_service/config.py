"""SMTP settings loaded from src/global_email_service/.env."""

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


@dataclass
class EmailServiceConfig:
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    use_tls: bool = True
    use_ssl: bool = False
    timeout_seconds: float = 15.0

    from_email: str = "edp-alerts@localhost"
    from_name: str = "EDP Billing Alerts"

    default_to: List[str] = field(default_factory=list)
    default_cc: List[str] = field(default_factory=list)

    max_retries: int = 2
    retry_backoff_seconds: float = 2.0
    dry_run: bool = False


def load_email_config() -> EmailServiceConfig:
    return EmailServiceConfig(
        smtp_host=os.getenv("EMAIL_SMTP_HOST", "localhost"),
        smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "587")),
        smtp_username=os.getenv("EMAIL_SMTP_USERNAME") or None,
        smtp_password=os.getenv("EMAIL_SMTP_PASSWORD") or None,
        use_tls=_env_bool("EMAIL_SMTP_USE_TLS", True),
        use_ssl=_env_bool("EMAIL_SMTP_USE_SSL", False),
        timeout_seconds=float(os.getenv("EMAIL_SMTP_TIMEOUT_SECONDS", "15")),
        from_email=os.getenv("EMAIL_FROM_ADDRESS", "edp-alerts@localhost"),
        from_name=os.getenv("EMAIL_FROM_NAME", "EDP Billing Alerts"),
        default_to=_split_addresses(os.getenv("EMAIL_DEFAULT_TO", "")),
        default_cc=_split_addresses(os.getenv("EMAIL_DEFAULT_CC", "")),
        max_retries=int(os.getenv("EMAIL_MAX_RETRIES", "2")),
        retry_backoff_seconds=float(os.getenv("EMAIL_RETRY_BACKOFF_SECONDS", "2")),
        dry_run=_env_bool("EMAIL_DRY_RUN", False),
    )
