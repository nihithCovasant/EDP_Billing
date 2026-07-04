"""JSON payload -> color-coded HTML table email over SMTP."""

from __future__ import annotations

from .config import EmailServiceConfig, load_email_config
from .exceptions import EmailServiceError, EmailSendError, InvalidPayloadError
from .service import EmailSendResult, send_alert_email, send_segment_alert

__all__ = [
    "send_alert_email",
    "send_segment_alert",
    "EmailSendResult",
    "EmailServiceConfig",
    "load_email_config",
    "EmailServiceError",
    "EmailSendError",
    "InvalidPayloadError",
]
