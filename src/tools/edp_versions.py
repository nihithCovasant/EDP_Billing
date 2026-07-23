"""
EDP workflow version lifecycle chat tools — active-version lookup, diffing,
and cloning, straight from the chat interface.

Plain HTTP clients against this same agent's own EDP API (GET/POST /edp/*),
exactly like edp_status.py and edpb_download.py. Deliberately no import of
src/agent/edp/** internals (including diff_workflow_configs() in
api/workflow.py), AND no cross-import from edp_status.py either — the tool
registry (src/tools/registry.py) loads each file in this directory via
spec_from_file_location rather than the normal package import machinery, so
this file carries its own small copies of the handful of shared helpers
(_get/_post, date normalization, base URL) instead of relying on import
ordering between dynamically-loaded sibling modules. Same duplication-over-
coupling convention edp_status.py already documents for _CODE_ALIASES.

Auto-discovered by the tool registry — no manual registration needed.
"""

from __future__ import annotations

import os
import re
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

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%B %d, %Y",
    "%b %d, %Y",
)
_ORDINAL_SUFFIX_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)
_RELATIVE_DAYS: dict[str, int] = {
    "today": 0,
    "yesterday": -1,
    "the day before yesterday": -2,
    "day before yesterday": -2,
    "tomorrow": 1,
}


def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def _normalize_date(raw: str) -> str:
    """Best-effort "YYYY-MM-DD" normalizer — see edp_status.py's identical
    helper docstring for the full rationale; duplicated here rather than
    imported (see module docstring)."""
    cleaned = raw.strip().lower()
    if cleaned in _RELATIVE_DAYS:
        target = datetime.now(IST).date() + timedelta(days=_RELATIVE_DAYS[cleaned])
        return target.isoformat()

    cleaned = _ORDINAL_SUFFIX_RE.sub("", raw.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _base_url() -> str:
    port = os.getenv("PORT", "8005")
    return f"http://localhost:{port}"


def _actor_headers() -> dict[str, str]:
    """Forward caller identity/role to this agent's own /edp/* API — see
    edp_status.py's identical helper docstring for the full rationale."""
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


_DIFF_WATCHED_FIELDS = ("window_start", "window_end", "login_id")

# Same currently-valid codes as api/workflow.py's _VALID_SEGMENT_CODES /
# _VALID_POST_TRADE_CODES (duplicated per this file's decoupling convention
# — see module docstring). If a saved version references a code outside
# these sets, that segment/post-trade process was removed from the active
# model after the version was saved (e.g. MF's removal — see constants.py)
# and would be silently ignored, not applied, if reapplied today.
_VALID_SEGMENT_CODES = frozenset(
    {
        "EQ",
        "DR",
        "CUR",
        "SLB",
        "NCDEX",
        "NCDEXPHY",
        "MCX",
        "MCXPHY",
        "NSECOM",
    }
)
_VALID_POST_TRADE_CODES = frozenset({"COLVAL", "COLALLOC", "MTFFT", "DMRPT", "DMSTMT"})


def _index_by_code(items: list[dict] | None, key: str) -> dict[str, dict]:
    return {item.get(key): item for item in (items or []) if isinstance(item, dict) and item.get(key)}


def _diff_section(old_items: list[dict] | None, new_items: list[dict] | None, key: str) -> list[dict]:
    old_by_code = _index_by_code(old_items, key)
    new_by_code = _index_by_code(new_items, key)
    changes: list[dict] = []
    for code, new_item in new_by_code.items():
        old_item = old_by_code.get(code)
        if old_item is None:
            changes.append({"code": code, "change": "added"})
            continue
        for field in _DIFF_WATCHED_FIELDS:
            if field in old_item or field in new_item:
                old_value, new_value = old_item.get(field), new_item.get(field)
                if old_value != new_value:
                    changes.append(
                        {
                            "code": code,
                            "change": "modified",
                            "field": field,
                            "old": old_value,
                            "new": new_value,
                        }
                    )
    for code in old_by_code:
        if code not in new_by_code:
            changes.append({"code": code, "change": "removed"})
    return changes


def _diff_workflow_json(old_json: dict | None, new_json: dict) -> str:
    """Human-readable summary of the differences between two workflow_json
    configs — same watched fields (window_start/window_end/login_id) as the
    upload-time audit diff, but usable on any two configs, not just an
    upload's before/after."""
    if old_json is None:
        return "No prior config to compare against (this would be the first upload)."

    segment_changes = _diff_section(old_json.get("segments"), new_json.get("segments"), "segment_code")
    process_changes = _diff_section(
        old_json.get("post_trade_processes"),
        new_json.get("post_trade_processes"),
        "process_code",
    )
    all_changes = segment_changes + process_changes
    if not all_changes:
        return (
            "No differences — the two configs are identical (segments and post-trade "
            "processes match on window_start/window_end/login_id)."
        )

    lines = []
    for c in all_changes:
        if c["change"] == "added":
            lines.append(f"- **{c['code']}**: added")
        elif c["change"] == "removed":
            lines.append(f"- **{c['code']}**: removed")
        else:
            lines.append(f"- **{c['code']}**.{c['field']}: `{c['old']}` → `{c['new']}`")
    return "\n".join(lines)


async def _fetch_named_version_json(version_name: str) -> tuple[dict | None, str | None]:
    """Returns (workflow_json, error_message) — workflow_json is None if the
    named version wasn't found or the fetch failed, with error_message set."""
    status_code, data = await _get(f"/edp/workflow/versions/{version_name}")
    if status_code == 404:
        return None, f"No saved version named **{version_name}**."
    if status_code >= 400:
        return None, f"❌ Could not fetch version **{version_name}** (HTTP {status_code}): {data.get('detail', data)}"
    return data.get("workflow_json"), None


async def _fetch_active_json(trade_date: str) -> tuple[dict | None, str | None, str | None]:
    """Returns (workflow_json, active_version_name, error_message)."""
    status_code, data = await _get(f"/edp/workflow/{trade_date}")
    if status_code == 404:
        return None, None, f"No active workflow config for **{trade_date}**."
    if status_code >= 400:
        return (
            None,
            None,
            f"❌ Could not fetch the active config for **{trade_date}** "
            f"(HTTP {status_code}): {data.get('detail', data)}",
        )
    return data.get("workflow_json"), data.get("version_name"), None


@tool
async def get_edp_active_version(trade_date: str | None = None) -> str:
    """
    Show which saved EDP workflow version is currently active for a trading
    date, and whether it was carried forward from an earlier upload. Use
    this when the user asks "which version is live right now", "what
    config is active today", "is today running the default config or
    something custom", or asks the same about a past date.

    `trade_date` is optional (any date phrasing, e.g. "today", "yesterday",
    "2026-07-10") — defaults to today (IST) if not mentioned.
    """
    resolved_date = _normalize_date(trade_date) if trade_date else _today_ist()
    status_code, data = await _get(f"/edp/workflow/{resolved_date}")
    if status_code == 404:
        return f"No active workflow config for **{resolved_date}**."
    if status_code >= 400:
        return f"❌ Could not fetch the active config (HTTP {status_code}): {data.get('detail', data)}"

    version_name = data.get("version_name") or "(unnamed)"
    carried_note = ""
    if data.get("carried_forward"):
        carried_note = (
            f"\n⚠️ Carried forward — no config was uploaded specifically for **{resolved_date}**; "
            f"this is the most recent config from an earlier date, still in effect."
        )
    return (
        f"### 📌 Active version for {resolved_date}\n\n"
        f"- **Version name:** {version_name}\n"
        f"- **Segments configured:** {data.get('segment_count')}\n"
        f"- **Post-trade processes:** {data.get('post_trade_process_count')}\n"
        f"- **Uploaded by:** {data.get('uploaded_by')}\n"
        f"- **Uploaded at:** {data.get('uploaded_at')}"
        f"{carried_note}"
    )


@tool
async def diff_edp_workflow_versions(
    version_a: str,
    version_b: str | None = None,
    trade_date: str | None = None,
) -> str:
    """
    Compare two saved EDP workflow versions and show exactly what differs —
    added/removed segments or post-trade processes, and any changed
    window_start/window_end/login_id. Use this whenever the user asks to
    "diff", "compare", "what's different between", or "preview/simulate
    what would change if I applied" a saved version.

    `version_a` is the saved version name to inspect (required). `version_b`
    is optional — if given, compares version_a against that other saved
    version; if omitted, compares version_a against whatever config is
    CURRENTLY ACTIVE for `trade_date` (defaults to today) WITHOUT applying
    anything — this is the safe "preview before applying" / "simulate"
    path, so always use this instead of apply_edp_workflow_version when the
    user just wants to see what would change.
    """
    json_a, err = await _fetch_named_version_json(version_a)
    if err:
        return err

    if version_b:
        json_b, err = await _fetch_named_version_json(version_b)
        if err:
            return err
        label_b = f"**{version_b}**"
    else:
        resolved_date = _normalize_date(trade_date) if trade_date else _today_ist()
        json_b, active_name, err = await _fetch_active_json(resolved_date)
        if err:
            return err
        label_b = f"the config currently active on **{resolved_date}** ({active_name or 'unnamed'})"

    diff_text = _diff_workflow_json(json_a, json_b)
    return f"### 🔍 Diff: **{version_a}** vs {label_b}\n\n{diff_text}"


@tool
async def clone_edp_workflow_version(
    source_version_name: str,
    new_version_name: str,
    uploaded_by: str | None = None,
    overwrite_version: bool = False,
) -> str:
    """
    Save a copy of an existing EDP workflow version under a new name,
    applying it now. Use this when the user asks to "clone", "duplicate",
    or "start a new version from" an existing saved config — e.g. "clone
    diwali_2025 as diwali_2026 and tweak it from there".

    `source_version_name` is the existing saved version to copy from.
    `new_version_name` is the label to save the clone under — REQUIRED,
    same rule as upload_edp_workflow_config (ask the user if not given, do
    not invent one). If `new_version_name` is already taken, this fails
    unless overwrite_version=True is explicitly confirmed by the user.
    Applies the cloned config starting now (subject to the same
    already-processing-today deferral rule as any other upload).
    """
    source_json, err = await _fetch_named_version_json(source_version_name)
    if err:
        return err

    logger.info(f"[EDP_CHAT] cloning workflow version {source_version_name!r} as {new_version_name!r}")
    status_code, data = await _post(
        "/edp/workflow/upload",
        {
            "workflow_json": source_json,
            "uploaded_by": uploaded_by or "chat-user",
            "version_name": new_version_name,
            "overwrite_version": overwrite_version,
        },
    )
    if status_code == 409:
        return (
            f"❌ A saved version named **{new_version_name}** already exists. "
            f"Please choose a different name, or confirm you want to overwrite it."
        )
    if status_code >= 400:
        return f"❌ Clone failed (HTTP {status_code}): {data.get('detail', data)}"

    deferred_note = (
        f"\n⚠️ Deferred: today's processing already started, so this was applied to "
        f"**{data.get('trade_date')}** instead of {data.get('resolved_trade_date')}."
        if data.get("deferred")
        else ""
    )
    return (
        f"✅ Cloned **{source_version_name}** as **{data.get('version_name')}** "
        f"and applied it for **{data.get('trade_date')}**.{deferred_note}"
    )


@tool
async def check_edp_version_name_reuse(version_name: str, limit: int = 200) -> str:
    """
    Check whether a given saved-config name has been used more than once
    across UNRELATED changes over time (e.g. the same version_name applied
    by different people on very different dates, which may indicate
    accidental reuse of a name rather than a deliberate re-application of
    the same config). Use this when the user asks "has this version name
    been reused", "who else has used the name X", or as a sanity check
    before naming a new config.

    `version_name` is the name to check. `limit` caps how many recent audit
    log entries are scanned (default 200, the API's own max) — this is a
    best-effort heuristic over recent history, not an exhaustive audit; if
    the name was used further back than the scanned window, this won't see
    it.
    """
    status_code, data = await _get(f"/edp/audit?limit={limit}")
    if status_code >= 400:
        return f"❌ Could not fetch the audit log (HTTP {status_code}): {data.get('detail', data)}"

    matches = [e for e in (data or []) if e.get("version_name") == version_name]
    if not matches:
        return (
            f"No audit history found for version name **{version_name}** in the last "
            f"{limit} entries — looks unused (within that window)."
        )
    if len(matches) == 1:
        m = matches[0]
        return (
            f"**{version_name}** has only ONE recorded use — by {m.get('actor')} on "
            f"{m.get('occurred_at')} ({m.get('trade_date') or 'no date'}). No reuse detected."
        )

    distinct_actors = {m.get("actor") for m in matches}
    lines = [
        f"### 🔁 **{version_name}** — {len(matches)} recorded uses",
        "",
        "| When | Actor | Trade date | Summary |",
        "|---|---|---|---|",
    ]
    for m in matches:
        lines.append(
            f"| {m.get('occurred_at')} | {m.get('actor')} | {m.get('trade_date') or '—'} | {m.get('summary')} |"
        )
    if len(distinct_actors) > 1:
        lines.append(
            f"\n⚠️ Used by **{len(distinct_actors)} different actors** ({', '.join(sorted(distinct_actors))}) "
            f"— worth confirming these are all genuinely the same intended config, not accidental name reuse."
        )
    return "\n".join(lines)


@tool
async def check_edp_version_segment_validity(version_name: str) -> str:
    """
    Check whether a saved EDP workflow version references any segment or
    post-trade process code that no longer exists in the current active
    model (e.g. a segment removed since the version was saved). Use this
    before applying an older saved version, when the user asks "is this
    version still valid", or "will this old config still work".

    `version_name` is the saved version to check. Upload-time validation
    already rejects unknown codes for NEW uploads, but a version saved
    before a code was removed is never re-checked afterwards — this tool
    re-validates it on demand instead.
    """
    workflow_json, err = await _fetch_named_version_json(version_name)
    if err:
        return err

    stale_segments = [
        s.get("segment_code")
        for s in (workflow_json.get("segments") or [])
        if s.get("segment_code") not in _VALID_SEGMENT_CODES
    ]
    stale_processes = [
        p.get("process_code")
        for p in (workflow_json.get("post_trade_processes") or [])
        if p.get("process_code") not in _VALID_POST_TRADE_CODES
    ]

    if not stale_segments and not stale_processes:
        return f"✅ **{version_name}** — all segment/process codes are still valid in the current model."

    lines = [f"⚠️ **{version_name}** references codes no longer in the current active model:"]
    if stale_segments:
        lines.append(f"- Segments: {', '.join(stale_segments)}")
    if stale_processes:
        lines.append(f"- Post-trade processes: {', '.join(stale_processes)}")
    lines.append(
        "\nIf reapplied as-is, these entries would be silently ignored rather than processed — "
        "consider removing them before applying this version, or confirm with the user first."
    )
    return "\n".join(lines)
