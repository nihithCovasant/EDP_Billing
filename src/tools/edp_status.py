"""
EDP billing chat tools — upload a workflow config and check segment status,
straight from the chat interface.

Both tools are plain HTTP clients against this same agent's own EDP API
(POST/GET /edp/*, see src/agent/edp/api/*.py) — exactly like an external
caller would use it. Deliberately no import of src/agent/edp/** internals,
so this stays fully decoupled from the EDP wake loop/state machine (same
convention as src/tools/edpb_download.py).

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import httpx
from langchain_core.tools import tool
from cams_otel_lib import Logger as logger

IST = ZoneInfo("Asia/Kolkata")

_STATUS_EMOJI = {
    "PENDING": "🕓",
    "IN_PROGRESS": "⏳",
    "COMPLETED": "✅",
    "SKIPPED": "⏭️",
    "FAILED": "❌",
}


def _base_url() -> str:
    """This same agent's own base URL — reachable at localhost regardless
    of the HOST it's bound to (0.0.0.0 isn't a valid client target)."""
    port = os.getenv("PORT", "8005")
    return f"http://localhost:{port}"


def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


async def _get(path: str) -> tuple[int, Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{_base_url()}{path}")
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:500]}


async def _post(path: str, json_body: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_base_url()}{path}", json=json_body)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:500]}


@tool
async def upload_edp_workflow_config(workflow_json: dict, uploaded_by: Optional[str] = None) -> str:
    """
    Upload an EDP billing workflow config for today. Use this when the user
    provides/pastes a workflow config JSON and asks to upload/apply it.

    `workflow_json` must contain a "segments" list (each item needs
    segment_code/login_id/window_start/window_end) and optionally a
    "post_trade_processes" list (each item needs process_code/login_id).
    There's no trade_date field — the server always resolves "today" itself,
    and if today's processing has already started, the upload is silently
    deferred to tomorrow instead of disrupting the in-flight run.
    """
    body = {"workflow_json": workflow_json, "uploaded_by": uploaded_by or "chat-user"}
    logger.info(f"[EDP_CHAT] uploading workflow config uploaded_by={body['uploaded_by']}")
    status_code, data = await _post("/edp/workflow/upload", body)

    if status_code >= 400:
        detail = data.get("detail", data)
        return f"❌ Workflow upload failed (HTTP {status_code}): {detail}"

    deferred_note = (
        f"\n⚠️ Deferred: today's processing already started, so this was applied to "
        f"**{data.get('trade_date')}** instead of {data.get('resolved_trade_date')}."
        if data.get("deferred")
        else ""
    )
    return (
        f"✅ Workflow config uploaded successfully.\n\n"
        f"- **Trade date:** {data.get('trade_date')}\n"
        f"- **Segments configured:** {data.get('segment_count')}\n"
        f"- **Post-trade processes configured:** {data.get('post_trade_process_count')}\n"
        f"- **Uploaded by:** {data.get('uploaded_by')}\n"
        f"- **Config ID:** {data.get('id')}"
        f"{deferred_note}"
    )


@tool
async def get_edp_status(trade_date: Optional[str] = None, segment_code: Optional[str] = None) -> str:
    """
    Check EDP billing processing status. Use this when the user asks about
    the status of a segment, a trading day, or "how is today's processing
    going".

    `trade_date` is optional, format YYYY-MM-DD — defaults to today (IST) if
    not mentioned. `segment_code` is optional (e.g. "EQ", "COLVAL") — if the
    user names a specific segment/process, pass it to get its full detail;
    if they ask about the whole day, leave it out to get a summary of every
    segment for that day.
    """
    resolved_date = trade_date or _today_ist()

    if segment_code:
        status_code, data = await _get(f"/edp/status/{resolved_date}/{segment_code.upper()}")
        if status_code == 404:
            return f"No record found for segment **{segment_code.upper()}** on **{resolved_date}**."
        if status_code >= 400:
            return f"❌ Could not fetch status (HTTP {status_code}): {data.get('detail', data)}"
        return _format_segment_detail(data)

    status_code, data = await _get(f"/edp/status/{resolved_date}")
    if status_code == 404:
        return f"No workflow has been processed for **{resolved_date}** yet."
    if status_code >= 400:
        return f"❌ Could not fetch status (HTTP {status_code}): {data.get('detail', data)}"
    return _format_day_summary(data)


def _format_day_summary(data: Dict[str, Any]) -> str:
    segments = sorted(data.get("segments", []), key=lambda s: s.get("sequence_order", 0))
    if not segments:
        return (
            f"No segments have started processing yet for **{data.get('trade_date')}** "
            f"(a workflow config may be active, but the wake loop hasn't picked up this date yet)."
        )
    lines = [
        f"### 📅 EDP Status — {data.get('trade_date')}",
        "",
        f"**Total:** {data.get('total')}  |  "
        f"🕓 Pending: {data.get('pending')}  |  "
        f"⏳ In progress: {data.get('in_progress')}  |  "
        f"✅ Completed: {data.get('completed')}  |  "
        f"⏭️ Skipped: {data.get('skipped')}  |  "
        f"❌ Failed: {data.get('failed')}",
        "",
        "| # | Segment | Status | Current step | Notes |",
        "|---|---------|--------|--------------|-------|",
    ]
    for seg in segments:
        status = seg.get("segment_status")
        emoji = _STATUS_EMOJI.get(status, "")
        current_step = (
            seg.get("current_process")
            or seg.get("current_state")
            or ("Not started" if status == "PENDING" else "Done" if status in ("COMPLETED", "SKIPPED") else "—")
        )
        note = seg.get("skip_reason") or ("STALE — no heartbeat recently" if seg.get("runtime_health") == "STALE" else "")
        lines.append(
            f"| {seg.get('sequence_order')} | {seg.get('segment_name')} ({seg.get('segment_code')}) "
            f"| {emoji} {status} | {current_step} | {note} |"
        )
    return "\n".join(lines)


def _format_segment_detail(data: Dict[str, Any]) -> str:
    emoji = _STATUS_EMOJI.get(data.get("segment_status"), "")
    lines = [
        f"### {emoji} {data.get('segment_name')} ({data.get('segment_code')}) — {data.get('trade_date')}",
        "",
        f"- **Status:** {data.get('segment_status')}",
        f"- **Current process / state:** {data.get('current_process') or '—'} / {data.get('current_state') or '—'}",
        f"- **Started at:** {data.get('started_at') or '—'}",
        f"- **Completed at:** {data.get('completed_at') or '—'}",
        f"- **Last heartbeat:** {data.get('last_heartbeat_at') or '—'} ({data.get('runtime_health') or 'unknown'})",
    ]
    if data.get("skip_category") or data.get("skip_reason"):
        lines.append(f"- **Skip/fail reason:** [{data.get('skip_category')}] {data.get('skip_reason')}")
    return "\n".join(lines)
