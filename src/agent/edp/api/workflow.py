"""
Workflow endpoints.

  POST   /edp/workflow/upload                     — upload workflow config (applies now)      [admin]
  GET    /edp/workflow/{trade_date}                — get active workflow for a date
  GET    /edp/workflow/{trade_date}/history        — all config versions for a date
  GET    /edp/workflow/versions                    — list all named versions
  GET    /edp/workflow/versions/{name}             — get one named version's full config
  POST   /edp/workflow/versions/{name}/apply       — re-apply a saved version now               [admin]
  DELETE /edp/workflow/versions/{name}             — un-name a version (soft delete the name)

require_admin_role (see ./auth.py) is scoped to exactly the two actions
that change what's running or when: uploading a config (which sets/edits
segment + post-trade window timings) and applying a saved version
(which changes which version is active). Everything else — listing,
reading, and un-naming a saved version — is unrestricted, since none of
those mutate timings or the active version.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace
from fastapi import APIRouter, Depends, HTTPException

from ..config import load_edp_config
from ..database import get_session
from ..models import AuditAction
from ..repository import (
    clear_version_name,
    get_active,
    get_by_version_name,
    get_latest_effective,
    get_workflow_history,
    has_processing_started,
    list_versions,
    record_audit_event,
    upload,
)
from ..utils.constants import POST_TRADE_ORDER, SEGMENT_ORDER
from ..utils.datetime_utils import now_ist, resolve_active_date
from .auth import require_admin_role
from .schemas import (
    WorkflowDetailResponse,
    WorkflowUploadRequest,
    WorkflowUploadResponse,
    WorkflowVersionApplyRequest,
    WorkflowVersionSummary,
)

try:
    from cams_otel_lib import get_request_context
except ImportError:  # pragma: no cover - defensive, see _resolve_actor()
    get_request_context = None

router = APIRouter()

_REQUIRED_SEGMENT_FIELDS = {"segment_code", "window_start", "window_end"}
_REQUIRED_POST_TRADE_FIELDS = {"process_code", "login_id"}
_VALID_SEGMENT_CODES = set(SEGMENT_ORDER)
_VALID_POST_TRADE_CODES = set(POST_TRADE_ORDER)
# Plain 24h HH:MM, e.g. "17:00", "06:00" — no seconds, no timezone (the
# whole config is IST-only, see EdpProperties docstring).
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_hhmm(value, label: str) -> None:
    if not isinstance(value, str) or not _HHMM_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"{label} must be a 24h 'HH:MM' string (e.g. '17:00'), got {value!r}",
        )


def _validate_workflow_json(workflow_json: dict) -> None:
    """
    Raise HTTPException(422) if workflow_json is missing required structure
    or contains values that would fail later at runtime (unknown segment
    codes, malformed window times, duplicate segments). Processing order is
    a fixed code constant, not part of the config. `post_trade_processes`
    is optional (backward compat — omitting it falls back to fixed legacy
    defaults); if present, validated like segments.
    """
    segments = workflow_json.get("segments")
    if not isinstance(segments, list) or len(segments) == 0:
        raise HTTPException(
            status_code=422,
            detail="workflow_json must contain a non-empty 'segments' list",
        )
    seen_segment_codes = set()
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise HTTPException(
                status_code=422,
                detail=f"segments[{i}] must be an object",
            )
        missing = _REQUIRED_SEGMENT_FIELDS - set(seg.keys())
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Segment[{i}] ({seg.get('segment_code', '?')}) missing required fields: {sorted(missing)}",
            )
        code = seg.get("segment_code", "")
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(
                status_code=422,
                detail=f"Segment[{i}] has an empty or invalid segment_code",
            )
        if code not in _VALID_SEGMENT_CODES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Segment[{i}] has unknown segment_code {code!r} — must be one of {sorted(_VALID_SEGMENT_CODES)}"
                ),
            )
        if code in seen_segment_codes:
            raise HTTPException(
                status_code=422,
                detail=f"Segment[{i}] duplicate segment_code {code!r}",
            )
        seen_segment_codes.add(code)
        _validate_hhmm(seg.get("window_start"), f"Segment[{i}] ({code}).window_start")
        _validate_hhmm(seg.get("window_end"), f"Segment[{i}] ({code}).window_end")

    post_trade_processes = workflow_json.get("post_trade_processes")
    if post_trade_processes is not None:
        if not isinstance(post_trade_processes, list):
            raise HTTPException(
                status_code=422,
                detail="workflow_json.post_trade_processes must be a list if present",
            )
        seen_codes = set()
        for i, proc in enumerate(post_trade_processes):
            if not isinstance(proc, dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"post_trade_processes[{i}] must be an object",
                )
            missing = _REQUIRED_POST_TRADE_FIELDS - set(proc.keys())
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"post_trade_processes[{i}] ({proc.get('process_code', '?')}) "
                        f"missing required fields: {sorted(missing)}"
                    ),
                )
            code = proc.get("process_code", "")
            if code not in _VALID_POST_TRADE_CODES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"post_trade_processes[{i}] has unknown process_code {code!r} "
                        f"— must be one of {sorted(_VALID_POST_TRADE_CODES)}"
                    ),
                )
            if code in seen_codes:
                raise HTTPException(
                    status_code=422,
                    detail=f"post_trade_processes[{i}] duplicate process_code {code!r}",
                )
            seen_codes.add(code)
            # window_start/window_end are optional per-process overrides here
            # (unlike segments, where they're required) — only shape-check
            # them when actually present.
            if proc.get("window_start") is not None:
                _validate_hhmm(proc.get("window_start"), f"post_trade_processes[{i}] ({code}).window_start")
            if proc.get("window_end") is not None:
                _validate_hhmm(proc.get("window_end"), f"post_trade_processes[{i}] ({code}).window_end")


def _resolve_actor(explicit: str) -> str:
    """
    Prefer the real caller identity from the request context (X-User-ID
    header -> OtelContextMiddleware, see src/middleware/claims_middleware.py)
    over the request body's self-reported `uploaded_by`, which any caller
    can set to anything. Falls back to `explicit` when no request-scoped
    identity is available (e.g. a direct repository call in a test/script,
    or a version of cams-otel-lib that predates get_request_context()).
    """
    if get_request_context is not None:
        try:
            ctx = get_request_context()
        except Exception:
            ctx = None
        userid = getattr(ctx, "userid", None) if ctx else None
        if userid and userid != "N/A":
            return userid
    return explicit or "unknown"


def _index_by_code(items: list, key: str) -> dict:
    return {item.get(key): item for item in (items or []) if isinstance(item, dict) and item.get(key)}


_DIFF_WATCHED_FIELDS = ("window_start", "window_end", "login_id")


def _diff_section(old_items: list, new_items: list, key: str) -> list[dict]:
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


def diff_workflow_configs(old_json: dict | None, new_json: dict) -> tuple[str, dict]:
    """
    Compare two workflow_json configs and return (human summary, structured
    diff) for the audit log — segments keyed by segment_code, post_trade_
    processes keyed by process_code. old_json=None (first-ever upload for a
    date) is reported as an initial config, not a "change".
    """
    if old_json is None:
        return "Initial config for this trade date", {"initial": True, "segments": [], "post_trade_processes": []}

    seg_changes = _diff_section(old_json.get("segments"), new_json.get("segments"), "segment_code")
    pt_changes = _diff_section(
        old_json.get("post_trade_processes"),
        new_json.get("post_trade_processes"),
        "process_code",
    )
    all_changes = seg_changes + pt_changes
    if not all_changes:
        return "Re-uploaded with no effective changes", {"segments": seg_changes, "post_trade_processes": pt_changes}

    modified = [c for c in all_changes if c["change"] == "modified"]
    added = [c["code"] for c in all_changes if c["change"] == "added"]
    removed = [c["code"] for c in all_changes if c["change"] == "removed"]
    parts = [f"{c['code']}.{c['field']} {c['old']!r}\u2192{c['new']!r}" for c in modified]
    bits = []
    if parts:
        bits.append(", ".join(parts))
    if added:
        bits.append(f"added {', '.join(added)}")
    if removed:
        bits.append(f"removed {', '.join(removed)}")
    return "; ".join(bits), {"segments": seg_changes, "post_trade_processes": pt_changes}


async def _upload_workflow_for_date(
    today: date,
    workflow_json: dict,
    uploaded_by: str,
    version_name: str | None = None,
    overwrite_version: bool = False,
) -> dict:
    """
    Core upload logic given an already-resolved "today" — split out from
    upload_workflow() so tests can drive it with an explicit test date.

    Every upload creates a brand-new row (no content-hash dedup). If
    today's trading date already has processing underway, the upload is
    deferred to tomorrow instead of disrupting the in-flight run; a day
    where every segment is still PENDING applies the change immediately.
    """
    actor = _resolve_actor(uploaded_by)
    async with get_session() as session:
        deferred = await has_processing_started(session, today)
        effective_date = today + timedelta(days=1) if deferred else today

        prior = await get_active(session, effective_date) or await get_latest_effective(session, effective_date)
        old_workflow_json = prior.workflow_json if prior else None

        try:
            row, is_new = await upload(
                session,
                effective_date,
                workflow_json,
                uploaded_by=uploaded_by,
                version_name=version_name,
                overwrite_version=overwrite_version,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if is_new:
            summary, changes = diff_workflow_configs(old_workflow_json, workflow_json)
            await record_audit_event(
                session,
                actor=actor,
                action=AuditAction.WORKFLOW_UPLOAD,
                summary=summary,
                trade_date=effective_date,
                version_name=row.version_name,
                config_id=row.id,
                changes=changes,
            )
    if deferred:
        logger.warning(
            f"POST /workflow/upload: {today} already has processing underway — "
            f"deferring config to {effective_date} instead of disrupting today's in-flight run"
        )
    post_trade_processes = workflow_json.get("post_trade_processes")
    logger.info(
        f"POST /workflow/upload: today={today} effective_date={effective_date} "
        f"deferred={deferred} is_new={is_new} segments={len(workflow_json.get('segments', []))} "
        f"post_trade_processes={len(post_trade_processes) if post_trade_processes is not None else 'default'}"
    )
    return {
        "id": row.id,
        "trade_date": row.trade_date,
        "is_active": row.is_active,
        "is_new": is_new,
        "deferred": deferred,
        "resolved_trade_date": today,
        "uploaded_by": row.uploaded_by,
        "uploaded_at": row.uploaded_at,
        "segment_count": len(row.workflow_json.get("segments", [])),
        "post_trade_process_count": (len(post_trade_processes) if post_trade_processes is not None else None),
        "version_name": row.version_name,
    }


@router.post("/workflow/upload", response_model=WorkflowUploadResponse, dependencies=[Depends(require_admin_role)])
@otel_trace
async def upload_workflow(body: WorkflowUploadRequest):
    """
    Upload the workflow config — always applies starting now.

    No trade_date input: the server resolves "today's trading date" itself
    (resolve_active_date(), same as the orchestrator's wake cycle), so ops
    can't target the wrong date. See _upload_workflow_for_date() for the
    deferral rule applied once "today" is resolved.

    Pass version_name to also save this config under a reusable label —
    see GET/POST /workflow/versions/* to list and re-apply saved configs
    later. If the name is already taken by another config, this returns
    409 unless overwrite_version=true.
    """
    _validate_workflow_json(body.workflow_json)
    config = load_edp_config()
    today = resolve_active_date(now_ist(), config.active_date_cutoff_hour, config.timezone)
    return await _upload_workflow_for_date(
        today,
        body.workflow_json,
        body.uploaded_by,
        version_name=body.version_name,
        overwrite_version=body.overwrite_version,
    )


def _version_summary(row) -> dict:
    post_trade_processes = row.workflow_json.get("post_trade_processes")
    return {
        "id": row.id,
        "version_name": row.version_name,
        "trade_date": row.trade_date,
        "is_active": row.is_active,
        "uploaded_by": row.uploaded_by,
        "uploaded_at": row.uploaded_at,
        "segment_count": len(row.workflow_json.get("segments", [])),
        "post_trade_process_count": (len(post_trade_processes) if post_trade_processes is not None else None),
    }


# NOTE: these /workflow/versions* routes must be declared before
# /workflow/{trade_date} below — Starlette matches routes in declaration
# order, and "versions" would otherwise be swallowed by {trade_date} first
# (then fail 422 trying to parse "versions" as a date) instead of ever
# reaching these.
@router.get("/workflow/versions", response_model=list[WorkflowVersionSummary])
@otel_trace
async def list_workflow_versions():
    """All saved named configs (not tied to any single trade_date), newest first."""
    async with get_session() as session:
        rows = await list_versions(session)
    return [_version_summary(r) for r in rows]


@router.get("/workflow/versions/{version_name}", response_model=WorkflowDetailResponse)
@otel_trace
async def get_workflow_version(version_name: str):
    """Get the full saved config for one named version."""
    async with get_session() as session:
        row = await get_by_version_name(session, version_name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No saved version named {version_name!r}")
    return {
        **_version_summary(row),
        "workflow_json": row.workflow_json,
        "requested_trade_date": None,
        "carried_forward": False,
    }


@router.post(
    "/workflow/versions/{version_name}/apply",
    response_model=WorkflowUploadResponse,
    dependencies=[Depends(require_admin_role)],
)
@otel_trace
async def apply_workflow_version(version_name: str, body: WorkflowVersionApplyRequest):
    """
    Re-apply a saved named config starting now.

    If this saved version is already today's active config (e.g. it's
    "default" and nothing else has been applied since), this is a no-op —
    it does NOT create a duplicate row, it just confirms it's already
    active. Otherwise it creates a brand-new edpb_properties row (a fresh
    audit entry, same as any other upload) for today's/tomorrow's trading
    date using the saved workflow_json verbatim, and MOVES the name onto
    that new row — "default" (or whatever name) always continues to point
    at whichever row is the currently-active applied config, rather than
    staying stuck on the old superseded one.
    """
    config = load_edp_config()
    today = resolve_active_date(now_ist(), config.active_date_cutoff_hour, config.timezone)
    async with get_session() as session:
        saved = await get_by_version_name(session, version_name)
        if saved is None:
            raise HTTPException(status_code=404, detail=f"No saved version named {version_name!r}")
        current_active = await get_active(session, today)
        if current_active is not None and current_active.id == saved.id:
            post_trade_processes = saved.workflow_json.get("post_trade_processes")
            return {
                "id": saved.id,
                "trade_date": saved.trade_date,
                "is_active": True,
                "is_new": False,
                "deferred": False,
                "resolved_trade_date": today,
                "uploaded_by": saved.uploaded_by,
                "uploaded_at": saved.uploaded_at,
                "segment_count": len(saved.workflow_json.get("segments", [])),
                "post_trade_process_count": (len(post_trade_processes) if post_trade_processes is not None else None),
                "version_name": saved.version_name,
            }
    return await _upload_workflow_for_date(
        today,
        saved.workflow_json,
        body.uploaded_by,
        version_name=version_name,
        overwrite_version=True,
    )


@router.delete("/workflow/versions/{version_name}")
@otel_trace
async def delete_workflow_version(version_name: str):
    """
    Un-name a saved version (soft delete — only clears the name, the
    underlying config row and its audit history are untouched).

    Not admin-gated: this only removes a label, it neither changes which
    version is active nor alters any segment/post-trade timings — the two
    actions require_admin_role is scoped to (see upload_workflow /
    apply_workflow_version below).
    """
    async with get_session() as session:
        removed = await clear_version_name(session, version_name)
        if removed:
            await record_audit_event(
                session,
                actor=_resolve_actor("ops"),
                action=AuditAction.WORKFLOW_VERSION_DELETE,
                summary=f"Un-named saved version {version_name!r}",
                version_name=version_name,
            )
    if not removed:
        raise HTTPException(status_code=404, detail=f"No saved version named {version_name!r}")
    return {"version_name": version_name, "deleted": True}


@router.get("/workflow/{trade_date}", response_model=WorkflowDetailResponse)
@otel_trace
async def get_workflow(trade_date: date, effective: bool = True):
    """
    Get the workflow config that governs a given trading date.

    By default (`effective=true`, matches what the orchestrator actually
    runs — see orchestrator._process_one_segment()'s own get_active() then
    get_latest_effective() fallback): if nothing was ever explicitly
    uploaded for `trade_date`, falls back to the most recently uploaded
    config on or before it (config "carries forward" day to day until
    superseded by a new upload). Pass `effective=false` for the strict,
    exact-date-only lookup (404s if `trade_date` has no upload of its own).
    """
    async with get_session() as session:
        row = await get_active(session, trade_date)
        if not row and effective:
            row = await get_latest_effective(session, trade_date)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No active workflow config for {trade_date}",
        )
    return {
        "id": row.id,
        "trade_date": row.trade_date,
        "is_active": row.is_active,
        "uploaded_by": row.uploaded_by,
        "uploaded_at": row.uploaded_at,
        "segment_count": len(row.workflow_json.get("segments", [])),
        "post_trade_process_count": (
            len(row.workflow_json["post_trade_processes"]) if "post_trade_processes" in row.workflow_json else None
        ),
        "workflow_json": row.workflow_json,
        "requested_trade_date": trade_date,
        "carried_forward": row.trade_date != trade_date,
        "version_name": row.version_name,
    }


@router.get("/workflow/{trade_date}/history")
@otel_trace
async def get_workflow_history_endpoint(trade_date: date):
    """
    All workflow config versions for a date (newest first).
    Useful for auditing config changes.
    """
    async with get_session() as session:
        rows = await get_workflow_history(session, trade_date)
    return [
        {
            "id": r.id,
            "trade_date": r.trade_date.isoformat(),
            "is_active": r.is_active,
            "uploaded_by": r.uploaded_by,
            "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
            "superseded_at": r.superseded_at.isoformat() if r.superseded_at else None,
            "segment_count": len(r.workflow_json.get("segments", [])),
        }
        for r in rows
    ]
