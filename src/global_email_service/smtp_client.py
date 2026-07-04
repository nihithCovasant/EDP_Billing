"""SMTP send with retry on transient connection errors."""

from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage
from typing import List, Optional, Sequence

from .config import EmailServiceConfig

logger = logging.getLogger("global_email_service")

# smtplib.SMTPException subclasses OSError — match SMTP errors before OSError.
_TRANSIENT_SMTP_EXCEPTIONS = (
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPHeloError,
)
_TRANSIENT_SOCKET_EXCEPTIONS = (TimeoutError, ConnectionError, OSError)


def build_message(
    config: EmailServiceConfig,
    to: Sequence[str],
    subject: str,
    html_body: str,
    text_body: str,
    cc: Optional[Sequence[str]] = None,
    bcc: Optional[Sequence[str]] = None,
) -> tuple[EmailMessage, List[str]]:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{config.from_name} <{config.from_email}>" if config.from_name else config.from_email
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    all_recipients = list(to) + list(cc or []) + list(bcc or [])
    return msg, all_recipients


def send_message(
    config: EmailServiceConfig,
    msg: EmailMessage,
    all_recipients: Sequence[str],
) -> None:
    last_exc: Optional[Exception] = None
    attempts = config.max_retries + 1

    for attempt in range(1, attempts + 1):
        try:
            _send_once(config, msg, all_recipients)
            return
        except _TRANSIENT_SMTP_EXCEPTIONS as exc:
            last_exc = exc
            logger.warning(
                "[global_email_service] SMTP send attempt %d/%d failed (transient SMTP error): %s",
                attempt, attempts, exc,
            )
        except smtplib.SMTPException as exc:
            logger.error("[global_email_service] SMTP send failed (non-transient): %s", exc)
            raise
        except _TRANSIENT_SOCKET_EXCEPTIONS as exc:
            last_exc = exc
            logger.warning(
                "[global_email_service] SMTP send attempt %d/%d failed (transient network error): %s",
                attempt, attempts, exc,
            )

        if attempt < attempts:
            time.sleep(config.retry_backoff_seconds)

    assert last_exc is not None
    raise last_exc


def _send_once(config: EmailServiceConfig, msg: EmailMessage, all_recipients: Sequence[str]) -> None:
    smtp_cls = smtplib.SMTP_SSL if config.use_ssl else smtplib.SMTP
    with smtp_cls(config.smtp_host, config.smtp_port, timeout=config.timeout_seconds) as server:
        if config.use_tls and not config.use_ssl:
            server.starttls()
        if config.smtp_username and config.smtp_password:
            server.login(config.smtp_username, config.smtp_password)
        server.send_message(msg, from_addr=config.from_email, to_addrs=list(all_recipients))
