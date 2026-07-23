"""
EDP operational control chat tools — skip/retry a segment for today, and
start/stop/check the 24/7 agent itself, straight from the chat interface.

Wraps three already-built but previously chat-unexposed endpoint groups:
  POST /edp/status/{trade_date}/{segment_code}/skip
  POST /edp/status/{trade_date}/{segment_code}/retry
  POST /edp/agent/start · POST /edp/agent/stop · GET /edp/agent/status

Plain HTTP client against this same agent's own EDP API, same convention as
edp_status.py/edp_versions.py — duplicated small helper set rather than a
cross-import between dynamically-loaded tool files (see edp_versions.py's
module docstring for why).

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from langchain_core.tools import tool

try:
    from cams_otel_lib import get_request_context
except ImportError:  # pragma: no cover - defensive, older cams-otel-lib
    get_request_context = None

try:
    from src.middleware.claims_middleware import get_current_role
except ImportError:  # pragma: no cover - defensive
    get_current_role = None

IST = ZoneInfo("Asia/Kolkata")

_CODE_ALIASES: dict[str, str] = {
    "EQ": "EQ",
    "CASH": "EQ",
    "EQUITY": "EQ",
    "DR": "DR",
    "F&O": "DR",
    "FO": "DR",
    "FNO": "DR",
    "DERIVATIVES": "DR",
    "CUR": "CUR",
    "CD": "CUR",
    "CURRENCY": "CUR",
    "SLB": "SLB",
    "NCDEX": "NCDEX",
    "NCDEXPHY": "NCDEXPHY",
    "NCDEX PHY": "NCDEXPHY",
    "NCDEX PHYSICAL": "NCDEXPHY",
    "MCX": "MCX",
    "MCXPHY": "MCXPHY",
    "MCX PHY": "MCXPHY",
    "MCX PHYSICAL": "MCXPHY",
    "NSECOM": "NSECOM",
    "NSE COMMODITY": "NSECOM",
    "COMMODITY": "NSECOM",
    "COLVAL": "COLVAL",
    "COLLATERAL VALUATION": "COLVAL",
    "COLALLOC": "COLALLOC",
    "COLLATERAL ALLOCATION": "COLALLOC",
    "MTFFT": "MTFFT",
    "MTF FUND TRANSFER": "MTFFT",
    "MTF": "MTFFT",
    "DMRPT": "DMRPT",
    "DAILY MARGIN REPORTING": "DMRPT",
    "DMSTMT": "DMSTMT",
    "DAILY MARGIN STATEMENTS": "DMSTMT",
}


def _resolve_code(identifier: str) -> str:
    return _CODE_ALIASES.get(identifier.strip().upper()) or identifier.strip().upper()


def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def _normalize_date(raw: str) -> str | None:
    cleaned = raw.strip().lower()
    relative = {"today": 0, "yesterday": -1, "tomorrow": 1}
    if cleaned in relative:
        return (datetime.now(IST).date() + timedelta(days=relative[cleaned])).isoformat()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _base_url() -> str:
    port = os.getenv("PORT", "8005")
    return f"http://localhost:{port}"


def _actor_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if get_request_context is not None:
        try:
            ctx = get_request_context()
        except Exception:
            ctx = None
        userid = getattr(ctx, "userid", None) if ctx else None
        if userid and userid != "N/A":
            headers["X-User-ID"] = userid
    if get_current_role is not None:
        try:
            role = get_current_role()
        except Exception:
            role = None
        if role:
            headers["X-User-Role"] = role
    return headers


async def _get(path: str) -> tuple[int, dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{_base_url()}{path}", headers=_actor_headers())
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:500]}


async def _post(path: str, json_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_base_url()}{path}", json=json_body, headers=_actor_headers())
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:500]}


@tool
async def skip_edp_segment_today(
    identifier: str,
    reason: str,
    trade_date: str | None = None,
    skipped_by: str | None = None,
) -> str:
    """
    Manually skip a segment or post-trade process for one trading day only
    — a one-off override, without touching any saved workflow config. Use
    this when the user says a segment should be "skipped today", "bypassed
    for today only", or "exchange declared no trades for this segment
    today" — NOT for permanently changing a segment's schedule (that's
    update_edp_segment_window).

    `identifier` is the segment/process code or common name (e.g. "EQ",
    "Cash", "COLVAL"). `reason` is REQUIRED — always ask the user why if
    they haven't said, so it's recorded for the audit trail. `trade_date`
    defaults to today (IST). Only works on a PENDING or IN_PROGRESS segment
    — already-terminal segments (COMPLETED/SKIPPED/FAILED) can't be
    re-skipped this way.
    """
    code = _resolve_code(identifier)
    resolved_date = _normalize_date(trade_date) if trade_date else _today_ist()
    status_code, data = await _post(
        f"/edp/status/{resolved_date}/{code}/skip",
        {"reason": reason, "skipped_by": skipped_by or "chat-user"},
    )
    if status_code == 404:
        return f"No record found for **{code}** on **{resolved_date}** — nothing to skip."
    if status_code == 409:
        return (
            f"❌ Can't skip **{code}** on **{resolved_date}** — it's already in a terminal state "
            f"(completed/skipped/failed), so a manual skip doesn't apply."
        )
    if status_code >= 400:
        return f"❌ Skip failed (HTTP {status_code}): {data.get('detail', data)}"
    return (
        f"✅ **{code}** marked SKIPPED for **{resolved_date}**.\n"
        f"- **Reason:** {data.get('skip_reason') or reason}\n"
        f"- **Category:** {data.get('skip_category') or '—'}"
    )


@tool
async def retry_edp_segment(identifier: str, trade_date: str | None = None) -> str:
    """
    Reset a FAILED or SKIPPED segment/post-trade process back to PENDING so
    the agent retries it on its next wake cycle. Use this after a transient
    CBOS outage, a missed window, or once a manual data fix has been made
    and the user asks to "retry", "re-run", or "try again" a segment.

    `identifier` is the segment/process code or common name. `trade_date`
    defaults to today (IST). Only works if the segment is currently FAILED
    or SKIPPED — a PENDING/IN_PROGRESS/COMPLETED segment can't be retried
    this way.
    """
    code = _resolve_code(identifier)
    resolved_date = _normalize_date(trade_date) if trade_date else _today_ist()
    status_code, data = await _post(f"/edp/status/{resolved_date}/{code}/retry", {})
    if status_code == 404:
        return f"No record found for **{code}** on **{resolved_date}** — nothing to retry."
    if status_code == 409:
        return (
            f"❌ Can't retry **{code}** on **{resolved_date}** — it isn't currently FAILED or "
            f"SKIPPED (retry only applies to those two states)."
        )
    if status_code >= 400:
        return f"❌ Retry failed (HTTP {status_code}): {data.get('detail', data)}"
    return f"✅ **{code}** reset to PENDING for **{resolved_date}** — will be picked up on the next wake cycle."


@tool
async def control_edp_agent(
    action: str,
    reason: str | None = None,
    requested_by: str | None = None,
) -> str:
    """
    Start, stop, or check the status of the EDP billing agent's 24/7
    processing loop — this is a holiday/maintenance switch, NOT a way to
    skip one segment (use skip_edp_segment_today for that). Use this when
    the user asks to "stop the agent for the market holiday", "resume
    processing", or "is the agent currently running".

    `action` must be one of: "start", "stop", "status". `reason` is
    strongly recommended for start/stop (e.g. "Diwali holiday", "emergency
    DB maintenance") so it's recorded in the control history — ask the
    user for one if they haven't given it. Not required for "status".
    """
    normalized = action.strip().lower()
    if normalized not in ("start", "stop", "status"):
        return f"❌ Unrecognized action **{action}** — must be one of: start, stop, status."

    if normalized == "status":
        status_code, data = await _get("/edp/agent/status")
        if status_code >= 400:
            return f"❌ Could not fetch agent status (HTTP {status_code}): {data.get('detail', data)}"
        lines = [f"### 🕹️ EDP agent state: **{data.get('effective_state')}**", ""]
        history = data.get("history") or []
        if history:
            lines.append("| When | Action | State | By | Reason |")
            lines.append("|---|---|---|---|---|")
            for h in history[:5]:
                lines.append(
                    f"| {h.get('requested_at')} | {h.get('action')} | {h.get('effective_state')} | "
                    f"{h.get('requested_by')} | {h.get('reason') or '—'} |"
                )
        return "\n".join(lines)

    body = {"requested_by": requested_by or "chat-user", "reason": reason}
    status_code, data = await _post(f"/edp/agent/{normalized}", body)
    if status_code >= 400:
        return f"❌ Agent {normalized} failed (HTTP {status_code}): {data.get('detail', data)}"

    if normalized == "stop":
        snapshot = data.get("snapshot") or {}
        active_note = ""
        if snapshot.get("active_segment"):
            active_note = (
                f"\n- **In-flight at stop time:** {snapshot.get('active_segment')} "
                f"({snapshot.get('active_process') or '—'} / {snapshot.get('active_state') or '—'})"
            )
        return (
            f"🛑 Agent **STOPPED** by {data.get('requested_by')}.\n"
            f"- **Reason:** {data.get('reason') or '—'}"
            f"{active_note}"
        )
    return f"▶️ Agent **STARTED** (resumed) by {data.get('requested_by')}.\n- **Reason:** {data.get('reason') or '—'}"
