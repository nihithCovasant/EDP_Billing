"""
EDP segment/process window chat tools operating on MULTIPLE targets in one
upload — bulk update, copy-window, and a full-day timeline view.

update_edp_segment_window (edp_status.py) only patches one identifier per
call, each issuing its own GET+POST round trip; calling it in a loop from
here would re-fetch and re-upload the whole config once per target (wasteful,
and creates one audit-log/version row per target instead of one combined
change). This file instead fetches the active config ONCE, patches every
target in-memory, then uploads ONCE — same fetch-patch-upload shape as
update_edp_segment_window, just batched.

Plain HTTP client against this same agent's own EDP API, same convention as
the other edp_*.py tool files — duplicated small helper set rather than a
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
from cams_otel_lib import Logger as logger
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

_TIME_FORMATS = ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H.%M")
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")
_RELATIVE_DAYS: dict[str, int] = {"today": 0, "yesterday": -1, "tomorrow": 1}


def _resolve_code(identifier: str) -> str | None:
    return _CODE_ALIASES.get(identifier.strip().upper())


def _normalize_time(raw: str) -> str:
    raw = raw.strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw.upper(), fmt).strftime("%H:%M")
        except ValueError:
            continue
    return raw


def _normalize_date(raw: str) -> str:
    cleaned = raw.strip().lower()
    if cleaned in _RELATIVE_DAYS:
        return (datetime.now(IST).date() + timedelta(days=_RELATIVE_DAYS[cleaned])).isoformat()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


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


def _find_target(workflow_json: dict, code: str) -> dict | None:
    for seg in workflow_json.get("segments", []) or []:
        if seg.get("segment_code") == code:
            return seg
    for proc in workflow_json.get("post_trade_processes", []) or []:
        if proc.get("process_code") == code:
            return proc
    return None


async def _fetch_todays_active_config() -> tuple[dict | None, str | None, str | None]:
    """Returns (workflow_json, carried_from_note, error_message). Refuses
    (returns error) if today's config can't be resolved — same today-only
    restriction as update_edp_segment_window, for the same reason: the
    upload endpoint always applies to today's active config."""
    resolved_date = _today_ist()
    status_code, data = await _get(f"/edp/workflow/{resolved_date}")
    if status_code == 404:
        return (
            None,
            None,
            (
                f"No workflow config found for **{resolved_date}** (nor any earlier date to carry "
                f"forward) — nothing to update."
            ),
        )
    if status_code >= 400:
        return None, None, f"❌ Could not fetch the current config (HTTP {status_code}): {data.get('detail', data)}"
    carried_note = f" (carried forward from **{data.get('trade_date')}**)" if data.get("carried_forward") else ""
    return data.get("workflow_json"), carried_note, None


@tool
async def update_edp_segment_windows_bulk(
    updates: list[dict[str, str]],
    version_name: str,
    uploaded_by: str | None = None,
    overwrite_version: bool = False,
) -> str:
    """
    Change several segments'/post-trade processes' start and/or end times in
    ONE combined config change. Use this whenever the user asks to update
    MORE THAN ONE segment/process in the same request — e.g. "push EQ to
    5pm and DR to 5:30pm", "move all the commodity segments 30 minutes
    later". For a single segment, prefer update_edp_segment_window instead.

    `updates` is a list of dicts, one per target, each with:
      - "identifier" (required): segment/process code or common name
      - "window_start" and/or "window_end" (at least one required per item)
    Example: [{"identifier": "EQ", "window_start": "17:00"},
              {"identifier": "DR", "window_start": "17:30", "window_end": "18:30"}]

    `version_name` is REQUIRED — same naming rule as every other config-
    saving tool; ask the user if not given. Applies to TODAY's active
    config only (same restriction as update_edp_segment_window) — all
    targets are patched and saved together as a single upload/version.
    """
    if not updates:
        return "Please tell me which segments/processes to update, and their new times."

    unresolved = []
    for u in updates:
        if not _resolve_code(u.get("identifier", "")):
            unresolved.append(u.get("identifier"))
        elif not u.get("window_start") and not u.get("window_end"):
            unresolved.append(u.get("identifier"))
    if unresolved:
        return (
            f"❌ Couldn't process: {', '.join(str(u) for u in unresolved)} — each entry needs a "
            f"recognized identifier and at least one of window_start/window_end."
        )

    workflow_json, carried_note, err = await _fetch_todays_active_config()
    if err:
        return err

    applied = []
    missing = []
    for u in updates:
        code = _resolve_code(u["identifier"])
        target = _find_target(workflow_json, code)
        if target is None:
            missing.append(code)
            continue
        changes = []
        if u.get("window_start"):
            new_start = _normalize_time(u["window_start"])
            target["window_start"] = new_start
            changes.append(f"start→{new_start}")
        if u.get("window_end"):
            new_end = _normalize_time(u["window_end"])
            target["window_end"] = new_end
            changes.append(f"end→{new_end}")
        applied.append(f"**{code}** ({', '.join(changes)})")

    if not applied:
        return f"❌ None of the requested targets ({', '.join(missing)}) exist in today's active config."

    logger.info(f"[EDP_CHAT] bulk-updating {len(applied)} targets version_name={version_name!r}")
    status_code, data = await _post(
        "/edp/workflow/upload",
        {
            "workflow_json": workflow_json,
            "uploaded_by": uploaded_by or "chat-user",
            "version_name": version_name,
            "overwrite_version": overwrite_version,
        },
    )
    if status_code == 409:
        return (
            f"❌ A saved version named **{version_name}** already exists. "
            f"Please choose a different name, or confirm you want to overwrite it."
        )
    if status_code >= 400:
        return f"❌ Bulk update failed (HTTP {status_code}): {data.get('detail', data)}"

    missing_note = f"\n⚠️ Not found, skipped: {', '.join(missing)}" if missing else ""
    deferred_note = (
        f"\n⚠️ Deferred: today's processing already started, so this was applied to "
        f"**{data.get('trade_date')}** instead."
        if data.get("deferred")
        else ""
    )
    return (
        f"✅ Updated {', '.join(applied)} and saved as **{data.get('version_name')}** "
        f"for **{data.get('trade_date')}**{carried_note}.{missing_note}{deferred_note}"
    )


@tool
async def copy_edp_segment_window(
    source_identifier: str,
    target_identifier: str,
    version_name: str,
    uploaded_by: str | None = None,
    overwrite_version: bool = False,
) -> str:
    """
    Copy one segment's/post-trade process's window (start and end time)
    onto another, in today's active config. Use this when the user asks to
    "copy CASH's window onto DR", "give NCDEX the same times as MCX", etc.

    `source_identifier` is the segment/process to copy FROM.
    `target_identifier` is the one to copy the window ONTO. `version_name`
    is REQUIRED — same naming rule as every other config-saving tool.
    Applies to TODAY's active config only.
    """
    source_code = _resolve_code(source_identifier)
    target_code = _resolve_code(target_identifier)
    if not source_code or not target_code:
        bad = source_identifier if not source_code else target_identifier
        return f'I don\'t recognize "{bad}" as a segment or post-trade process.'

    workflow_json, carried_note, err = await _fetch_todays_active_config()
    if err:
        return err

    source = _find_target(workflow_json, source_code)
    if source is None:
        return f"**{source_code}** isn't present in today's active config — nothing to copy from."
    target = _find_target(workflow_json, target_code)
    if target is None:
        return f"**{target_code}** isn't present in today's active config — nothing to copy onto."

    target["window_start"] = source.get("window_start")
    target["window_end"] = source.get("window_end")

    logger.info(f"[EDP_CHAT] copying window {source_code}->{target_code} version_name={version_name!r}")
    status_code, data = await _post(
        "/edp/workflow/upload",
        {
            "workflow_json": workflow_json,
            "uploaded_by": uploaded_by or "chat-user",
            "version_name": version_name,
            "overwrite_version": overwrite_version,
        },
    )
    if status_code == 409:
        return (
            f"❌ A saved version named **{version_name}** already exists. "
            f"Please choose a different name, or confirm you want to overwrite it."
        )
    if status_code >= 400:
        return f"❌ Copy failed (HTTP {status_code}): {data.get('detail', data)}"

    deferred_note = (
        f"\n⚠️ Deferred: today's processing already started, so this was applied to "
        f"**{data.get('trade_date')}** instead."
        if data.get("deferred")
        else ""
    )
    return (
        f"✅ Copied **{source_code}**'s window (`{source.get('window_start')}`-`{source.get('window_end')}`) "
        f"onto **{target_code}**, saved as **{data.get('version_name')}** for "
        f"**{data.get('trade_date')}**{carried_note}.{deferred_note}"
    )


@tool
async def get_edp_day_timeline(trade_date: str | None = None) -> str:
    """
    Show all configured segments' and post-trade processes' windows for a
    trading day as one sorted timeline table. Use this when the user asks
    to "see the whole day's schedule", "show me the timeline", or "what
    runs when today" — a single view instead of checking each segment
    individually.

    `trade_date` is optional (any date phrasing) — defaults to today (IST).
    """
    resolved_date = _normalize_date(trade_date) if trade_date else _today_ist()
    status_code, data = await _get(f"/edp/workflow/{resolved_date}")
    if status_code == 404:
        return f"No active workflow config for **{resolved_date}**."
    if status_code >= 400:
        return f"❌ Could not fetch the config (HTTP {status_code}): {data.get('detail', data)}"

    workflow_json = data.get("workflow_json") or {}
    rows = []
    for seg in workflow_json.get("segments", []) or []:
        rows.append(("segment", seg.get("segment_code"), seg.get("window_start"), seg.get("window_end")))
    for proc in workflow_json.get("post_trade_processes", []) or []:
        rows.append(("post-trade", proc.get("process_code"), proc.get("window_start"), proc.get("window_end")))

    if not rows:
        return f"No segments or post-trade processes configured for **{resolved_date}**."

    rows.sort(key=lambda r: (r[2] or "", r[0]))

    lines = [
        f"### 🗓️ Timeline — {resolved_date}",
        "",
        "| Start | End | Code | Type |",
        "|---|---|---|---|",
    ]
    for kind, code, start, end in rows:
        lines.append(f"| {start or '—'} | {end or '—'} | **{code}** | {kind} |")
    return "\n".join(lines)
