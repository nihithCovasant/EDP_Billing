"""
Ops email alerts — thin async wrapper around the standalone
`global_email_service` library (see global_email_service/README.md).

Fires on:
  - FAILED  (any segment or post-trade process — always a real error,
    halts the rest of the day's chain, needs a human to look at it)
  - TIMEOUT (window deadline missed/exceeded — an operational concern,
    CBOS/exchange files never showed up in time)
  - SKIPPED (any other SKIP — market holiday via CBOS_SKIP, or a manual
    ops override — ops explicitly wants visibility into every segment
    that didn't run to completion, not just outright failures)

Deliberately does NOT fire on:
  - COMPLETED — success is the default expected outcome; the status API
    (GET /edp/status/{date}) is the place to check a normal day's results.

Never allowed to affect pipeline state: by the time any function here is
called, the row's terminal state (FAILED/SKIPPED) is already set on the
in-memory row (about to be committed by the caller) — any failure to
actually send (Graph misconfigured, network down, timeout) is caught and
logged as a WARNING, never raised, so a broken mail server can't turn an
already-handled pipeline error into an unhandled one.

global_email_service.send_segment_alert() is synchronous (it calls
Microsoft Graph via plain blocking httpx, with retry+time.sleep backoff
under the hood) — run via asyncio.to_thread so a slow/unreachable mail
server blocks only this one coroutine, never the event loop that every
other segment/request shares.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

from cams_otel_lib import Logger as logger

try:
    from global_email_service import (
        EmailServiceError,
        send_segment_alert as _send_segment_alert,
    )
    _EMAIL_SERVICE_AVAILABLE = True
except ImportError:
    _EMAIL_SERVICE_AVAILABLE = False


def alerts_enabled() -> bool:
    """
    Single on/off switch, independent of global_email_service's own
    EMAIL_DRY_RUN (that controls whether a *configured* send actually
    calls Graph). This one controls whether EDP even attempts to call the
    library at all — default on, so a fresh deployment gets alerts (or at
    least DRY_RUN-logged ones) without extra setup, but easy to silence
    entirely (e.g. in tests, or a deployment with no ops mailbox yet).
    """
    return os.getenv("EDP_EMAIL_ALERTS_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def _send(row: Dict[str, Any], event: str) -> None:
    if not _EMAIL_SERVICE_AVAILABLE:
        logger.debug(
            f"[EDP ALERTS] global_email_service not installed — skipping {event} alert "
            f"for {row.get('segment_code')}"
        )
        return
    if not alerts_enabled():
        return

    try:
        result = await asyncio.to_thread(_send_segment_alert, row)
        logger.info(
            f"[EDP ALERTS] {event} alert sent — segment={row.get('segment_code')} "
            f"dry_run={result.dry_run} to={result.to}"
        )
    except EmailServiceError as exc:
        logger.warning(
            f"[EDP ALERTS] Failed to send {event} alert for {row.get('segment_code')}: {exc}"
        )
    except Exception as exc:
        logger.warning(
            f"[EDP ALERTS] Unexpected error sending {event} alert for "
            f"{row.get('segment_code')}: {exc}",
            exc_info=True,
        )


async def send_failure_alert(row: Dict[str, Any]) -> None:
    """Fire-and-forget (but awaited inline) alert for a segment/process reaching FAILED."""
    await _send(row, "FAILED")


async def send_timeout_alert(row: Dict[str, Any]) -> None:
    """Same, for a segment reaching SKIPPED via a missed/exceeded window deadline."""
    await _send(row, "TIMEOUT")


async def send_skip_alert(row: Dict[str, Any]) -> None:
    """
    Same, for a segment reaching SKIPPED via pipeline._skip() — market
    holiday (CBOS_SKIP) or any other non-timeout SKIP raised mid-stage.
    Kept as its own event (distinct log line from TIMEOUT) even though both
    end in SegmentStatus.SKIPPED, since the underlying cause/skip_category
    on the row itself is what actually distinguishes them for the reader.
    """
    await _send(row, "SKIPPED")


def describe_alert_config() -> str:
    """
    One-line startup summary of alerting state, logged once by
    EdpWakeLoop.start() — same spirit as the CBOS/DB config summary, so
    "are alerts actually going to fire" is answered at startup instead of
    only discovered the first time a segment fails.
    """
    if not _EMAIL_SERVICE_AVAILABLE:
        return "global_email_service not installed — alerts DISABLED"
    if not alerts_enabled():
        return "alerts DISABLED (EDP_EMAIL_ALERTS_ENABLED=false)"
    try:
        from global_email_service import load_email_config

        config = load_email_config()
        graph_configured = bool(
            config.graph_tenant_id and config.graph_client_id and config.graph_client_secret
        )
        if config.dry_run:
            return f"alerts ENABLED, DRY_RUN (would send to {config.default_to or 'no default recipients set'})"
        if not graph_configured:
            return "alerts ENABLED but Graph is NOT configured — sends will fail (set EMAIL_GRAPH_* or EMAIL_DRY_RUN=true)"
        return f"alerts ENABLED, sending via Graph as {config.graph_sender} to {config.default_to or 'no default recipients set'}"
    except Exception as exc:
        return f"alerts ENABLED but config failed to load: {exc}"
