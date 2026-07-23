"""
EDP system health chat tool — CBOS connectivity, wake-loop liveness,
database, alerting, and email dry-run status, straight from the chat
interface.

Wraps the already-built GET /edp/health endpoint (see
src/agent/__main__.py::edp_health_check, which itself composes
EdpWakeLoop.health_snapshot(), CbosClient.check_connectivity(), and
database.check_connectivity()) — previously chat-unexposed. Also reads
EMAIL_DRY_RUN directly from the process environment: global_email_service
is imported in-process by this same agent (see
src/agent/edp/repository/segment.py's send_segment_alert import), so this
env var is directly visible here, not a separate service's private config.

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import httpx
from langchain_core.tools import tool


def _base_url() -> str:
    port = os.getenv("PORT", "8005")
    return f"http://localhost:{port}"


async def _get(path: str) -> tuple[int, Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{_base_url()}{path}")
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:500]}


def _is_dry_run() -> bool:
    return os.getenv("EMAIL_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


@tool
async def check_edp_system_health() -> str:
    """
    Check whether the EDP billing agent's core dependencies are healthy
    right now — the 24/7 wake loop, the database, and CBOS connectivity.
    Use this when the user asks "are you healthy", "is everything running
    OK", "is CBOS reachable", "why isn't anything processing" — a quick
    real-time diagnostic straight from the running system, not a status
    report about any specific segment (use get_edp_status for that).
    """
    status_code, data = await _get("/edp/health")
    if status_code not in (200, 503):
        return f"❌ Could not reach the health endpoint (HTTP {status_code})."

    overall = data.get("status", "unknown")
    overall_emoji = "✅" if overall == "healthy" else "❌"

    loop = data.get("billing_loop", {}) or {}
    loop_emoji = "✅" if loop.get("running") and loop.get("alive") else "❌"

    db = data.get("database", {}) or {}
    db_emoji = "✅" if db.get("status") == "ok" else "❌"

    cbos = data.get("cbos", {}) or {}
    cbos_status = cbos.get("status", "unknown")
    cbos_emoji = "✅" if cbos_status in ("ok", "mock") else "❌"
    cbos_note = " (mock mode)" if cbos_status == "mock" else ""

    alerts = data.get("alerts", {}) or {}

    lines = [
        f"### {overall_emoji} EDP system health — **{overall.upper()}**",
        "",
        f"- {loop_emoji} **Wake loop:** {'running' if loop.get('running') else 'stopped'}"
        + (f" — {loop.get('alive_reason')}" if not loop.get("alive") else ""),
        f"- {db_emoji} **Database:** {db.get('status', 'unknown')}"
        + (f" — {db.get('error')}" if db.get("error") else ""),
        f"- {cbos_emoji} **CBOS:** {cbos_status}{cbos_note}",
    ]

    if alerts.get("last_attempt_at"):
        lines.append(
            f"- **Last alert attempt:** {alerts.get('last_attempt_at')} "
            f"(last success: {alerts.get('last_success_at') or 'never'}, "
            f"last failure: {alerts.get('last_failure_at') or 'never'})"
        )
    else:
        lines.append("- **Alerts:** none sent yet this run.")

    if _is_dry_run():
        lines.append(
            "- ⚠️ **Email alerting is in DRY-RUN mode** — alerts are logged/rendered but not "
            "actually sent. Set `EMAIL_DRY_RUN=false` to send real emails."
        )

    return "\n".join(lines)
