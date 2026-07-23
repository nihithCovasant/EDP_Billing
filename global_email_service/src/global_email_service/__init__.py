"""JSON payload -> color-coded HTML table email via Microsoft Graph."""

from __future__ import annotations

from .config import EmailServiceConfig, load_email_config
from .exceptions import EmailSendError, EmailServiceError, InvalidPayloadError
from .service import EmailSendResult, send_alert_email, send_segment_alert

__all__ = [
    "EmailSendError",
    "EmailSendResult",
    "EmailServiceConfig",
    "EmailServiceError",
    "InvalidPayloadError",
    "load_email_config",
    "send_alert_email",
    "send_segment_alert",
]
