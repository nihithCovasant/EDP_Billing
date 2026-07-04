"""
Workflow endpoints.

  POST /edp/workflow/upload              — upload daily workflow config
  GET  /edp/workflow/{trade_date}        — get active workflow for a date
  GET  /edp/workflow/{trade_date}/history — all config versions for a date
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException

from ..database import get_session
from ..repository import upload, get_active, get_workflow_history
from .schemas import WorkflowUploadRequest, WorkflowUploadResponse, WorkflowDetailResponse
from cams_otel_lib import Logger as logger, otel_trace

router = APIRouter()

_REQUIRED_SEGMENT_FIELDS = {"segment_code", "window_start", "window_end"}


def _validate_workflow_json(workflow_json: dict) -> None:
    """
    Raise HTTPException(422) if workflow_json is missing required structure.
    Called before writing to DB so bad configs are rejected early.

    Processing order is NOT part of the uploaded config — it is a fixed code
    constant (see utils/constants.SEGMENT_ORDER) and cannot be overridden
    per upload.
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


@router.post("/workflow/upload", response_model=WorkflowUploadResponse)
@otel_trace
async def upload_workflow(body: WorkflowUploadRequest):
    """
    Upload the workflow config for a trading date.

    - Identical config → returns existing row with is_new=False (no duplicate created).
    - Different config → supersedes old row and creates new (is_new=True).
    """
    _validate_workflow_json(body.workflow_json)
    async with get_session() as session:
        row, is_new = await upload(
            session,
            body.trade_date,
            body.workflow_json,
            uploaded_by=body.uploaded_by,
        )
    logger.info(
        f"POST /workflow/upload: date={body.trade_date} is_new={is_new} "
        f"segments={len(body.workflow_json.get('segments', []))}"
    )
    return {
        "id": row.id,
        "trade_date": row.trade_date,
        "content_hash": row.content_hash,
        "is_active": row.is_active,
        "is_new": is_new,
        "uploaded_by": row.uploaded_by,
        "uploaded_at": row.uploaded_at,
        "segment_count": len(row.workflow_json.get("segments", [])),
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
