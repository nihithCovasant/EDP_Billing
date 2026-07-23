"""Payload validation and send orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .colors import resolve_row_style
from .config import EmailServiceConfig, load_email_config
from .exceptions import EmailSendError, InvalidPayloadError
from .graph_client import send_message
from .table_renderer import render_email_body

logger = logging.getLogger("global_email_service")

_META_KEYS = {
    "rows",
    "row",
    "to",
    "cc",
    "bcc",
    "subject",
    "title",
    "summary",
    "severity_field",
    "columns",
    "color_overrides",
}


@dataclass
class AlertEmailRequest:
    rows: list[dict]
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: str | None = None
    title: str | None = None
    summary: str | None = None
    columns: list[str] | None = None
    color_overrides: dict[str, tuple] | None = None


@dataclass
class EmailSendResult:
    success: bool
    message: str
    subject: str
    to: list[str]
    cc: list[str] = field(default_factory=list)
    dry_run: bool = False


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


def parse_payload(payload: dict) -> AlertEmailRequest:
    if not isinstance(payload, dict):
        raise InvalidPayloadError("Payload must be a JSON object.")

    if "rows" in payload:
        rows = payload["rows"]
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise InvalidPayloadError("'rows' must be a list of JSON objects.")
    elif "row" in payload:
        row = payload["row"]
        if not isinstance(row, dict):
            raise InvalidPayloadError("'row' must be a JSON object.")
        rows = [row]
    else:
        flat_row = {k: v for k, v in payload.items() if k not in _META_KEYS}
        rows = [flat_row] if flat_row else []

    if not rows:
        raise InvalidPayloadError(
            "No row data found — provide 'rows' (list), 'row' (object), or top-level fields describing a single record."
        )

    return AlertEmailRequest(
        rows=rows,
        to=_as_list(payload.get("to")),
        cc=_as_list(payload.get("cc")),
        bcc=_as_list(payload.get("bcc")),
        subject=payload.get("subject"),
        title=payload.get("title"),
        summary=payload.get("summary"),
        columns=payload.get("columns"),
        color_overrides=payload.get("color_overrides"),
    )


def _default_subject(rows: list[dict]) -> str:
    if len(rows) == 1:
        row = rows[0]
        style = resolve_row_style(row)
        identifier = row.get("segment_code") or row.get("name") or row.get("id") or "Record"
        return f"EDP Alert: {identifier} - {style.label}"

    counts: dict[str, int] = {}
    for row in rows:
        label = resolve_row_style(row).label
        counts[label] = counts.get(label, 0) + 1
    breakdown = ", ".join(f"{v} {k}" for k, v in counts.items())
    return f"EDP Alert: {len(rows)} record(s) reported ({breakdown})"


def send_alert_email(
    payload: dict,
    config: EmailServiceConfig | None = None,
) -> EmailSendResult:
    config = config or load_email_config()
    request = parse_payload(payload)

    to = request.to or list(config.default_to)
    if not to:
        raise InvalidPayloadError("No recipients resolved — pass 'to' in the payload or set EMAIL_DEFAULT_TO.")
    cc = request.cc or list(config.default_cc)

    subject = request.subject or _default_subject(request.rows)
    html_body, _text_body = render_email_body(
        request.rows,
        title=request.title,
        summary=request.summary,
        columns=request.columns,
        color_overrides=request.color_overrides,
    )

    if config.dry_run:
        logger.info(
            "[global_email_service][DRY_RUN] subject=%r to=%s cc=%s rows=%d",
            subject,
            to,
            cc,
            len(request.rows),
        )
        logger.debug("[global_email_service][DRY_RUN] HTML body:\n%s", html_body)
        return EmailSendResult(
            success=True,
            message="dry_run — not actually sent",
            subject=subject,
            to=to,
            cc=cc,
            dry_run=True,
        )

    try:
        send_message(config, to=to, cc=cc, bcc=request.bcc, subject=subject, html_body=html_body)
    except EmailSendError:
        raise
    except Exception as exc:
        logger.error("[global_email_service] Failed to send alert email: %s", exc)
        raise EmailSendError(f"Failed to send alert email: {exc}") from exc

    logger.info(
        "[global_email_service] Alert email sent — subject=%r to=%s cc=%s rows=%d",
        subject,
        to,
        cc,
        len(request.rows),
    )
    return EmailSendResult(success=True, message="sent", subject=subject, to=to, cc=cc)


def send_segment_alert(
    row: dict,
    *,
    to: list[str] | None = None,
    subject: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    config: EmailServiceConfig | None = None,
) -> EmailSendResult:
    payload: dict[str, Any] = {"row": row}
    if to:
        payload["to"] = to
    if subject:
        payload["subject"] = subject
    if title:
        payload["title"] = title
    if summary:
        payload["summary"] = summary
    return send_alert_email(payload, config=config)
