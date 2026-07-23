"""
Audit log endpoints — read-only view of edpb_audit_log (config-change
history: workflow uploads, quick segment-window patches, named-version
deletes). See models.py::AuditLog for the exact scope.

  GET /edp/audit — recent audit entries, optionally filtered
"""

from __future__ import annotations

from datetime import date

from cams_otel_lib import otel_trace
from fastapi import APIRouter, Query

from ..database import get_session
from ..repository import get_audit_history
from .schemas import AuditLogEntry

router = APIRouter()


@router.get("/audit", response_model=list[AuditLogEntry])
@otel_trace
async def list_audit_log(
    trade_date: date | None = None,
    action: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Recent config-change audit entries, most recent first.

    `trade_date` filters to changes affecting that trading date;
    `action` filters to one action type (WORKFLOW_UPLOAD or
    WORKFLOW_VERSION_DELETE); `limit` caps the result (default 50, max 200).
    """
    async with get_session() as session:
        rows = await get_audit_history(session, trade_date=trade_date, action=action, limit=limit)
    return rows
