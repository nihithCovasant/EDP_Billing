"""Send email via Microsoft Graph `sendMail` (OAuth2 client-credentials).

The sender is a mailbox (`config.graph_sender`, e.g. `rms@covasant.com`)
that the Azure AD app registration has been granted `Mail.Send`
application permission for. Kept synchronous and dependency-free (plain
`httpx`) so this module stays standalone.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import httpx

from .config import EmailServiceConfig
from .exceptions import EmailSendError

logger = logging.getLogger("global_email_service")

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# Non-transient (auth/config/bad-request) HTTP statuses — never retried.
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 422})

_token_cache: dict[str, tuple[str, float]] = {}


def _require_graph_config(config: EmailServiceConfig) -> None:
    missing = [
        name
        for name, value in (
            ("EMAIL_GRAPH_TENANT_ID", config.graph_tenant_id),
            ("EMAIL_GRAPH_CLIENT_ID", config.graph_client_id),
            ("EMAIL_GRAPH_CLIENT_SECRET", config.graph_client_secret),
        )
        if not value
    ]
    if missing:
        raise EmailSendError(
            f"Microsoft Graph is not configured — missing {', '.join(missing)}. "
            "Set them in this project's .env or the process environment "
            "(or set EMAIL_DRY_RUN=true for local testing)."
        )


def _get_access_token(config: EmailServiceConfig) -> str:
    cache_key = f"{config.graph_tenant_id}:{config.graph_client_id}"
    cached = _token_cache.get(cache_key)
    if cached and time.monotonic() < cached[1] - 60:
        return cached[0]

    response = httpx.post(
        _TOKEN_URL.format(tenant=config.graph_tenant_id),
        data={
            "client_id": config.graph_client_id,
            "client_secret": config.graph_client_secret,
            "scope": _DEFAULT_SCOPE,
            "grant_type": "client_credentials",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=config.timeout_seconds,
    )
    if response.status_code != 200:
        raise EmailSendError(f"Graph token request failed: HTTP {response.status_code} — {response.text}")

    data = response.json()
    token = str(data["access_token"])
    _token_cache[cache_key] = (token, time.monotonic() + float(data.get("expires_in", 3600)))
    return token


def _build_message(
    *,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    subject: str,
    html_body: str,
) -> dict:
    return {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        "bccRecipients": [{"emailAddress": {"address": a}} for a in bcc],
    }


def send_message(
    config: EmailServiceConfig,
    *,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    subject: str,
    html_body: str,
) -> None:
    """Sends one HTML email via Graph `sendMail` from `config.graph_sender`.

    Retries transient failures (network/timeout/5xx) up to `config.max_retries`
    times with a linear backoff; auth/config/bad-request errors (400/401/403/
    404/422) fail immediately.
    """
    _require_graph_config(config)
    message = _build_message(to=to, cc=cc, bcc=bcc, subject=subject, html_body=html_body)

    last_exc: Exception | None = None
    attempts = config.max_retries + 1

    for attempt in range(1, attempts + 1):
        try:
            token = _get_access_token(config)
            response = httpx.post(
                f"{_GRAPH_BASE}/users/{config.graph_sender}/sendMail",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"message": message, "saveToSentItems": True},
                timeout=config.timeout_seconds,
            )
            if response.status_code in (200, 202):
                logger.info(
                    "[global_email_service] Graph sendMail succeeded — sender=%s to=%d cc=%d",
                    config.graph_sender,
                    len(to),
                    len(cc),
                )
                return
            if response.status_code in _PERMANENT_STATUSES:
                raise EmailSendError(
                    f"Graph sendMail failed (non-transient): HTTP {response.status_code} — {response.text}"
                )
            last_exc = EmailSendError(
                f"Graph sendMail failed (transient): HTTP {response.status_code} — {response.text}"
            )
            logger.warning(
                "[global_email_service] Graph sendMail attempt %d/%d failed: %s",
                attempt,
                attempts,
                last_exc,
            )
        except EmailSendError:
            raise
        except httpx.HTTPError as exc:
            last_exc = exc
            logger.warning(
                "[global_email_service] Graph sendMail attempt %d/%d failed (network error): %s",
                attempt,
                attempts,
                exc,
            )

        if attempt < attempts:
            time.sleep(config.retry_backoff_seconds)

    assert last_exc is not None
    raise last_exc if isinstance(last_exc, EmailSendError) else EmailSendError(str(last_exc))
