"""
Status endpoints.

  GET  /edp/status/{trade_date}                   — day summary (all segments)
  GET  /edp/status/{trade_date}/{segment_code}    — single segment detail + processes_json
  POST /edp/status/{trade_date}/{segment_code}/retry  — reset FAILED → PENDING
  POST /edp/status/{trade_date}/{segment_code}/skip   — manually skip a segment
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_session
from ..repository import get_day_summary, get_one, retry_segment, skip_segment_manually
from ..utils.serializers import serialize_segment
from .schemas import DaySummaryResponse, SegmentDetailResponse
from cams_otel_lib import Logger as logger, otel_trace

router = APIRouter()


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

@router.get("/status/{trade_date}", response_model=DaySummaryResponse)
@otel_trace
async def get_day_status(trade_date: date, domain: str = Query(default="EDP")):
    """
    Full day summary — all segments with their status, current process/phase,
    timing, and processes_json detail.

    Answers:
    - How many segments have completed / failed / are in progress?
    - Which segment is currently processing?
    - What is the poll count / trigger time per stage?
    """
    async with get_session() as session:
        return await get_day_summary(session, trade_date, domain)


@router.get("/status/{trade_date}/{segment_code}", response_model=SegmentDetailResponse)
@otel_trace
async def get_segment_status(
    trade_date: date,
    segment_code: str,
    domain: str = Query(default="EDP"),
):
    """
    Single segment detail — full processes_json, lock info, HITL alerts included.

    Answers:
    - Which stage of a segment failed and why?
    - When was the trigger sent exactly?
    - How many times did we poll BILLPOSTING before it completed?
    - Is there a stale lock (crash recovery)?
    """
    async with get_session() as session:
        row = await get_one(session, trade_date, segment_code, domain)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No execution record for segment={segment_code} "
                f"date={trade_date} domain={domain}"
            ),
        )
    return serialize_segment(row)


# ---------------------------------------------------------------------------
# Operational control endpoints
# ---------------------------------------------------------------------------

class SkipRequest(BaseModel):
    reason: str
    skipped_by: str = "ops"


@router.post("/status/{trade_date}/{segment_code}/retry")
@otel_trace
async def retry_failed_segment(
    trade_date: date,
    segment_code: str,
    domain: str = Query(default="EDP"),
):
    """
    Reset a FAILED segment back to PENDING so the pipeline retries it on the
    next wake cycle.

    Use after a transient CBOS outage or manual data fix.
    Only works if the segment is currently in FAILED status.
    """
    async with get_session() as session:
        row = await retry_segment(session, trade_date, segment_code, domain)
    if not row:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry segment={segment_code} on {trade_date}: "
                f"segment not found or not in FAILED status"
            ),
        )
    logger.info(f"Segment {segment_code} retried on {trade_date}")
    return {
        "segment_code": row.segment_code,
        "trade_date": row.trade_date.isoformat(),
        "segment_status": row.segment_status.value,
        "message": "Segment reset to PENDING — will be picked up on next wake cycle",
    }


@router.post("/status/{trade_date}/{segment_code}/skip")
@otel_trace
async def skip_segment(
    trade_date: date,
    segment_code: str,
    body: SkipRequest,
    domain: str = Query(default="EDP"),
):
    """
    Manually skip a PENDING or IN_PROGRESS segment.

    Use when a segment was already processed outside the agent, or must be
    bypassed for the day (e.g. exchange declared no trades for this segment).
    Cannot be applied to segments that are already COMPLETED / SKIPPED / FAILED.
    """
    async with get_session() as session:
        row = await skip_segment_manually(
            session, trade_date, segment_code,
            reason=body.reason,
            skipped_by=body.skipped_by,
            domain=domain,
        )
    if not row:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot skip segment={segment_code} on {trade_date}: "
                f"segment not found or already in a terminal state"
            ),
        )
    logger.info(f"Segment {segment_code} manually skipped on {trade_date} by {body.skipped_by}")
    return {
        "segment_code": row.segment_code,
        "trade_date": row.trade_date.isoformat(),
        "segment_status": row.segment_status.value,
        "skip_category": row.skip_category,
        "skip_reason": row.skip_reason,
        "message": "Segment marked SKIPPED — pipeline chain will continue to next segment",
    }
