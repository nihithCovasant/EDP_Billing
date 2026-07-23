"""
Agent control endpoints.

  POST /edp/agent/start   — resume the agent after a stop
  POST /edp/agent/stop    — stop the agent (holiday / maintenance)
  GET  /edp/agent/status  — current RUNNING / STOPPED state + recent history
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from ..config import load_edp_config
from ..database import get_session
from ..models import AgentControlAction
from ..repository import (
    get_effective_state,
    record_action,
    get_control_history,
    get_day_summary,
)
from ..utils.datetime_utils import resolve_active_date
from .schemas import (
    AgentControlRequest,
    AgentControlResponse,
    AgentStopResponse,
    AgentStatusResponse,
)
from cams_otel_lib import Logger as logger, otel_trace

router = APIRouter()


@router.post("/agent/start", response_model=AgentControlResponse)
@otel_trace
async def agent_start(body: AgentControlRequest):
    """Resume the agent after a holiday or maintenance stop."""
    async with get_session() as session:
        record = await record_action(
            session,
            AgentControlAction.START,
            body.requested_by,
            body.reason,
        )
    logger.info(f"Agent START requested by {body.requested_by}")
    return {
        "action": record.action.value,
        "effective_state": record.effective_state,
        "requested_at": record.requested_at.isoformat(),
        "requested_by": record.requested_by,
        "reason": record.reason,
    }


@router.post("/agent/stop", response_model=AgentStopResponse)
@otel_trace
async def agent_stop(body: AgentControlRequest):
    """
    Stop the agent (market holiday / emergency maintenance).
    Captures a snapshot of the current segment state for the audit log.
    """
    config = load_edp_config()
    snapshot: dict = {}

    try:
        now = datetime.now(ZoneInfo(config.timezone))
        active_date = resolve_active_date(
            now, config.active_date_cutoff_hour, config.timezone
        )
        async with get_session() as session:
            summary = await get_day_summary(session, active_date)
        snapshot = {
            "active_date": active_date.isoformat(),
            "total": summary["total"],
            "pending": summary["pending"],
            "in_progress": summary["in_progress"],
            "completed": summary["completed"],
            "skipped": summary["skipped"],
            "failed": summary["failed"],
        }
        for seg in summary.get("segments", []):
            if seg["segment_status"] == "IN_PROGRESS":
                snapshot["active_segment"] = seg["segment_code"]
                snapshot["active_process"] = seg.get("current_process")
                snapshot["active_state"] = seg.get("current_state")
                break
    except Exception as exc:
        logger.warning(f"Could not capture snapshot for agent stop: {exc}")

    async with get_session() as session:
        record = await record_action(
            session,
            AgentControlAction.STOP,
            body.requested_by,
            body.reason,
            snapshot=snapshot,
        )
    logger.info(f"Agent STOP requested by {body.requested_by}")
    return {
        "action": record.action.value,
        "effective_state": record.effective_state,
        "requested_at": record.requested_at.isoformat(),
        "requested_by": record.requested_by,
        "reason": record.reason,
        "snapshot": snapshot,
    }


@router.get("/agent/status", response_model=AgentStatusResponse)
@otel_trace
async def agent_status():
    """
    Current agent state (RUNNING or STOPPED) plus recent control history.
    Shows the last 10 START/STOP events for operational visibility.
    """
    async with get_session() as session:
        state = await get_effective_state(session)
        history_rows = await get_control_history(session, limit=10)
    return {
        "effective_state": state,
        "history": [
            {
                "action": r.action.value,
                "effective_state": r.effective_state,
                "requested_at": r.requested_at.isoformat(),
                "requested_by": r.requested_by,
                "reason": r.reason,
            }
            for r in history_rows
        ],
    }


# =============================================================================
# On-demand segment run (wayfinder ticket 13) — backfill / arbitrary trade date
# =============================================================================

from datetime import date as _date  # noqa: E402

from fastapi import HTTPException, Request  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from ..repository import activate_segment_run  # noqa: E402
from ..repository import workflow as workflow_repo  # noqa: E402
from ..utils.constants import SEGMENT_ORDER  # noqa: E402
from .auth import require_admin_role  # noqa: E402


class RunSegmentRequest(BaseModel):
    trade_date: _date = Field(description="Trade date to run (YYYY-MM-DD) — any date, not just today")
    segment_code: str = Field(description=f"One of {', '.join(SEGMENT_ORDER)}")


@router.post("/run", status_code=202)
@otel_trace
async def run_segment(body: RunSegmentRequest, request: Request):
    """Create-or-reset the (trade_date, segment) row and mark it
    manually_activated: the wake loop then drives it on its next cycle even
    though the date isn't the active one, with window gating bypassed
    (logged loudly). Admin-gated — this can start billing for an arbitrary
    day. A COMPLETED segment is NOT re-runnable here (409): re-running
    finished billing is the double-trigger disaster."""
    require_admin_role(request)

    code = body.segment_code.upper()
    if code not in SEGMENT_ORDER:
        raise HTTPException(status_code=404, detail=f"Unknown segment_code {body.segment_code!r}")

    async with get_session() as session:
        wf = await workflow_repo.get_active(session, body.trade_date)
        if not wf:
            wf = await workflow_repo.get_latest_effective(session, body.trade_date)
        if not wf:
            raise HTTPException(
                status_code=409,
                detail=f"No workflow config exists on or before {body.trade_date} — upload one first",
            )
        outcome, row = await activate_segment_run(session, wf, body.trade_date, code)
        if outcome == "completed":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{code} {body.trade_date} is COMPLETED — re-running finished billing is not "
                    "allowed via this endpoint"
                ),
            )
        await session.commit()

    logger.info(
        f"[OPS] segment={code} trade_date={body.trade_date} | POST /edp/run -> {outcome}"
    )
    return {
        "trade_date": body.trade_date.isoformat(),
        "segment_code": code,
        "outcome": outcome,
        "manually_activated": True,
        "note": "The wake loop drives this row on its next cycle; window gating is bypassed.",
    }
