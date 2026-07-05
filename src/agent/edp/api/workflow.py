"""
Workflow endpoints.

  POST /edp/workflow/upload              — upload daily workflow config
  GET  /edp/workflow/{trade_date}        — get active workflow for a date
  GET  /edp/workflow/{trade_date}/history — all config versions for a date
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

from ..database import get_session
from ..repository import upload, get_active, get_workflow_history, has_processing_started
from ..utils.constants import POST_TRADE_ORDER
from .schemas import WorkflowUploadRequest, WorkflowUploadResponse, WorkflowDetailResponse
from cams_otel_lib import Logger as logger, otel_trace

router = APIRouter()

_REQUIRED_SEGMENT_FIELDS = {"segment_code", "window_start", "window_end"}
_REQUIRED_POST_TRADE_FIELDS = {"process_code", "login_id"}
_VALID_POST_TRADE_CODES = set(POST_TRADE_ORDER)


def _validate_workflow_json(workflow_json: dict) -> None:
    """
    Raise HTTPException(422) if workflow_json is missing required structure.
    Called before writing to DB so bad configs are rejected early.

    Processing order is NOT part of the uploaded config for either segments
    or post_trade_processes — both are fixed code constants (see
    utils/constants.SEGMENT_ORDER / POST_TRADE_ORDER) and cannot be
    overridden per upload; process_code must be one of the 5 fixed
    POST_TRADE_ORDER values since CBOS trigger-endpoint dispatch is wired
    per code (no CBOS integration exists for an arbitrary 6th process).

    `post_trade_processes` is OPTIONAL for backward compatibility with
    configs uploaded before this became ops-configurable — omitting it
    entirely falls back to the fixed legacy defaults at seed/resolve time
    (see repository.segment.seed_post_trade_processes(),
    orchestrator._resolve_post_trade_window()). If present, it is validated
    the same way segments are.
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


@router.post("/workflow/upload", response_model=WorkflowUploadResponse)
@otel_trace
async def upload_workflow(body: WorkflowUploadRequest):
    """
    Upload the workflow config for a trading date.

    - Identical config → returns existing row with is_new=False (no duplicate created).
    - Different config → supersedes old row and creates new (is_new=True).

    Ops does NOT upload a config every day — only when something changes
    (segments, windows, login IDs). Because of that, an upload can arrive
    at any time, including while today's trade_date is already mid-run. To
    avoid a live config change disrupting segments that are already
    IN_PROGRESS/COMPLETED/SKIPPED/FAILED (window times / login IDs are
    resolved live from the active config every cycle — see
    orchestrator._resolve_window()), any upload targeting a trade_date that
    already has processing underway is automatically deferred to
    trade_date + 1: it is saved and will become active starting the next
    trading day, leaving today's in-flight run completely untouched.
    A trade_date where every segment is still PENDING (windows not open
    yet, or nothing seeded at all) is NOT considered "started" — same-day
    changes are applied immediately in that case.
    """
    _validate_workflow_json(body.workflow_json)
    requested_date = body.trade_date
    async with get_session() as session:
        deferred = await has_processing_started(session, requested_date)
        effective_date = requested_date + timedelta(days=1) if deferred else requested_date
        row, is_new = await upload(
            session,
            effective_date,
            body.workflow_json,
            uploaded_by=body.uploaded_by,
        )
    if deferred:
        logger.warning(
            f"POST /workflow/upload: {requested_date} already has processing underway — "
            f"deferring config to {effective_date} instead of disrupting today's in-flight run"
        )
    post_trade_processes = body.workflow_json.get("post_trade_processes")
    logger.info(
        f"POST /workflow/upload: requested_date={requested_date} effective_date={effective_date} "
        f"deferred={deferred} is_new={is_new} segments={len(body.workflow_json.get('segments', []))} "
        f"post_trade_processes={len(post_trade_processes) if post_trade_processes is not None else 'default'}"
    )
    return {
        "id": row.id,
        "trade_date": row.trade_date,
        "content_hash": row.content_hash,
        "is_active": row.is_active,
        "is_new": is_new,
        "deferred": deferred,
        "requested_trade_date": requested_date,
        "uploaded_by": row.uploaded_by,
        "uploaded_at": row.uploaded_at,
        "segment_count": len(row.workflow_json.get("segments", [])),
        "post_trade_process_count": (
            len(post_trade_processes) if post_trade_processes is not None else None
        ),
    }


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
        "content_hash": row.content_hash,
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
            "content_hash": r.content_hash,
            "is_active": r.is_active,
            "uploaded_by": r.uploaded_by,
            "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
            "superseded_at": r.superseded_at.isoformat() if r.superseded_at else None,
            "segment_count": len(r.workflow_json.get("segments", [])),
        }
        for r in rows
    ]
