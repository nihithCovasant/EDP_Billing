"""
Workflow endpoints.

  POST /edp/workflow/upload              — upload workflow config (applies now)
  GET  /edp/workflow/{trade_date}        — get active workflow for a date
  GET  /edp/workflow/{trade_date}/history — all config versions for a date
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

from ..config import load_edp_config
from ..database import get_session
from ..repository import upload, get_active, get_workflow_history, has_processing_started
from ..utils.constants import POST_TRADE_ORDER
from ..utils.datetime_utils import now_ist, resolve_active_date
from .schemas import WorkflowUploadRequest, WorkflowUploadResponse, WorkflowDetailResponse
from cams_otel_lib import Logger as logger, otel_trace

router = APIRouter()

_REQUIRED_SEGMENT_FIELDS = {"segment_code", "window_start", "window_end"}
_REQUIRED_POST_TRADE_FIELDS = {"process_code", "login_id"}
_VALID_POST_TRADE_CODES = set(POST_TRADE_ORDER)


def _validate_workflow_json(workflow_json: dict) -> None:
    """
    Raise HTTPException(422) if workflow_json is missing required structure.
    Processing order is a fixed code constant, not part of the config.
    `post_trade_processes` is optional (backward compat — omitting it falls
    back to fixed legacy defaults); if present, validated like segments.
    """
    segments = workflow_json.get("segments")
    if not isinstance(segments, list) or len(segments) == 0:
        raise HTTPException(
            status_code=422,
            detail="workflow_json must contain a non-empty 'segments' list",
        )
    for i, seg in enumerate(segments):
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


async def _upload_workflow_for_date(
    today: date,
    workflow_json: dict,
    uploaded_by: str,
) -> dict:
    """
    Core upload logic given an already-resolved "today" — split out from
    upload_workflow() so tests can drive it with an explicit test date.

    Every upload creates a brand-new row (no content-hash dedup). If
    today's trading date already has processing underway, the upload is
    deferred to tomorrow instead of disrupting the in-flight run; a day
    where every segment is still PENDING applies the change immediately.
    """
    async with get_session() as session:
        deferred = await has_processing_started(session, today)
        effective_date = today + timedelta(days=1) if deferred else today
        row, is_new = await upload(
            session,
            effective_date,
            workflow_json,
            uploaded_by=uploaded_by,
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
        "post_trade_process_count": (
            len(post_trade_processes) if post_trade_processes is not None else None
        ),
    }


@router.post("/workflow/upload", response_model=WorkflowUploadResponse)
@otel_trace
async def upload_workflow(body: WorkflowUploadRequest):
    """
    Upload the workflow config — always applies starting now.

    No trade_date input: the server resolves "today's trading date" itself
    (resolve_active_date(), same as the orchestrator's wake cycle), so ops
    can't target the wrong date. See _upload_workflow_for_date() for the
    deferral rule applied once "today" is resolved.
    """
    _validate_workflow_json(body.workflow_json)
    config = load_edp_config()
    today = resolve_active_date(now_ist(), config.active_date_cutoff_hour, config.timezone)
    return await _upload_workflow_for_date(today, body.workflow_json, body.uploaded_by)


@router.get("/workflow/{trade_date}", response_model=WorkflowDetailResponse)
@otel_trace
async def get_workflow(trade_date: date):
    """Get the currently active workflow config for a given trading date."""
    async with get_session() as session:
        row = await get_active(session, trade_date)
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
            len(row.workflow_json["post_trade_processes"])
            if "post_trade_processes" in row.workflow_json
            else None
        ),
        "workflow_json": row.workflow_json,
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
