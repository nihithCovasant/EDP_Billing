"""
edpb_agent_control table — START/STOP audit log operations.
"""

from __future__ import annotations

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AgentControl, AgentControlAction


@otel_trace
async def get_effective_state(session: AsyncSession) -> str:
    """Returns 'RUNNING' or 'STOPPED'. Defaults to RUNNING if no rows exist."""
    stmt = select(AgentControl).order_by(desc(AgentControl.requested_at)).limit(1)
    row = (await session.execute(stmt)).scalar_one_or_none()
    return row.effective_state if row else "RUNNING"


@otel_trace
async def record_action(
    session: AsyncSession,
    action: AgentControlAction,
    requested_by: str,
    reason: str | None = None,
    snapshot: dict | None = None,
) -> AgentControl:
    """
    Append a START or STOP record.
    snapshot captures the live segment state at time of action for audit.
    """
    effective = "RUNNING" if action == AgentControlAction.START else "STOPPED"
    record = AgentControl(
        action=action,
        requested_by=requested_by,
        reason=reason,
        effective_state=effective,
        snapshot_json=snapshot or {},
    )
    session.add(record)
    await session.flush()
    logger.info(f"AgentControl recorded: action={action.value} effective={effective} by={requested_by}")
    return record


@otel_trace
async def get_history(
    session: AsyncSession,
    limit: int = 20,
) -> list[AgentControl]:
    """Return recent agent control events, most recent first."""
    stmt = select(AgentControl).order_by(desc(AgentControl.requested_at)).limit(limit)
    return list((await session.execute(stmt)).scalars().all())
