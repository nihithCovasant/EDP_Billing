"""
Audit log endpoints — read-only view of edpb_audit_log (config-change
history: workflow uploads, quick segment-window patches, named-version
deletes). See models.py::AuditLog for the exact scope.

  GET /edp/audit — recent audit entries, optionally filtered
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..database import get_session
from ..models import AuditAction
from ..repository import get_audit_history
from .schemas import AuditLogEntry
from cams_otel_lib import otel_trace

router = APIRouter()


@router.get("/audit", response_model=list[AuditLogEntry])
@otel_trace
async def list_audit_log(
    trade_date: Optional[date] = None,
    action: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Recent config-change audit entries, most recent first.

    `trade_date` filters to changes affecting that trading date;
    `action` filters to one action type (WORKFLOW_UPLOAD or
    WORKFLOW_VERSION_DELETE); `limit` caps the result (default 50, max 200).
    """
    if action is not None:
        try:
            AuditAction(action)
        except ValueError:
            valid = ", ".join(a.value for a in AuditAction)
            raise HTTPException(
                status_code=422,
                detail=f"Invalid action={action!r} — must be one of: {valid}",
            )
    async with get_session() as session:
        rows = await get_audit_history(session, trade_date=trade_date, action=action, limit=limit)
    return rows
