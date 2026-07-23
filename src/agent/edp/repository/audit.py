"""
edpb_audit_log table — append-only record of config changes (see
models.py::AuditLog for the exact scope/rationale).
"""

from __future__ import annotations

from datetime import date

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditAction, AuditLog


@otel_trace
async def record_event(
    session: AsyncSession,
    actor: str,
    action: AuditAction,
    summary: str,
    trade_date: date | None = None,
    version_name: str | None = None,
    config_id: str | None = None,
    changes: dict | None = None,
) -> AuditLog:
    """Append one audit entry. Caller commits/flushes the outer session."""
    row = AuditLog(
        actor=actor,
        action=action,
        summary=summary,
        trade_date=trade_date,
        version_name=version_name,
        config_id=config_id,
        changes_json=changes or {},
    )
    session.add(row)
    await session.flush()
    logger.info(
        f"Audit: action={action.value} actor={actor} trade_date={trade_date} "
        f"version_name={version_name!r} summary={summary!r}"
    )
    return row


@otel_trace
async def get_history(
    session: AsyncSession,
    trade_date: date | None = None,
    action: str | None = None,
    limit: int = 50,
) -> list[AuditLog]:
    """Recent audit entries, most recent first, optionally filtered."""
    stmt = select(AuditLog).order_by(desc(AuditLog.occurred_at)).limit(limit)
    if trade_date is not None:
        stmt = stmt.where(AuditLog.trade_date == trade_date)
    if action is not None:
        # Accept either an AuditAction member or its raw string value (e.g.
        # from a query param) -- coerce so the Enum column comparison always
        # binds correctly regardless of which one the caller passed.
        stmt = stmt.where(AuditLog.action == AuditAction(action))
    return list((await session.execute(stmt)).scalars().all())
