"""
edpb_segment_execution table — CRUD, seeding, state transitions, heartbeat,
and queries.

No pod-to-pod locking (single-instance deployment) — an IN_PROGRESS row
resumes at its persisted current_phase on restart. The TRIGGERING
pre-commit marker (utils/json_helpers.py) still protects the CBOS trigger
call itself from double-firing.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    EdpProperties,
    SegmentExecution,
    SegmentPhase,
    SegmentStatus,
)
from ..utils.constants import get_sequence_order, POST_TRADE_ORDER
from ..utils.datetime_utils import now_ist
from cams_otel_lib import Logger as logger, otel_trace

# Terminal statuses — once here, a row is "handled" and won't be revisited.
_TERMINAL_STATUSES = (SegmentStatus.COMPLETED, SegmentStatus.SKIPPED, SegmentStatus.FAILED)


# =============================================================================
# Queries
# =============================================================================

@otel_trace
async def get_one(
    session: AsyncSession,
    trade_date: date,
    segment_code: str,
) -> Optional[SegmentExecution]:
    stmt = select(SegmentExecution).where(
        SegmentExecution.trade_date == trade_date,
        SegmentExecution.segment_code == segment_code,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@otel_trace
async def get_all_for_date(
    session: AsyncSession,
    trade_date: date,
) -> List[SegmentExecution]:
    """Return all segment rows for a date, ordered by the fixed SEGMENT_ORDER."""
    stmt = select(SegmentExecution).where(
        SegmentExecution.trade_date == trade_date,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    rows.sort(key=lambda r: get_sequence_order(r.segment_code))
    return rows


@otel_trace
async def get_in_progress(
    session: AsyncSession,
    trade_date: date,
) -> Optional[SegmentExecution]:
    """Return the segment currently IN_PROGRESS, if any."""
    stmt = select(SegmentExecution).where(
        SegmentExecution.trade_date == trade_date,
        SegmentExecution.segment_status == SegmentStatus.IN_PROGRESS,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# =============================================================================
# Seeding
# =============================================================================

@otel_trace
async def get_or_create(
    session: AsyncSession,
    workflow: EdpProperties,
    trade_date: date,
    segment_code: str,
) -> SegmentExecution:
    """
    Lazy per-segment record creation — creates the row only if missing,
    else reconciles config_id_used if it's still PENDING under a stale
    config reference. Called once per segment/process per cycle from the
    orchestrator, right before dispatching to a state handler.
    """
    row = await get_one(session, trade_date, segment_code)
    if row:
        if row.segment_status == SegmentStatus.PENDING and row.config_id_used != workflow.id:
            row.config_id_used = workflow.id
            await session.flush()
            logger.info(
                f"[OPS] segment={segment_code} trade_date={trade_date} | "
                f"Segment row's config reference updated (was PENDING)"
            )
        return row

    row = SegmentExecution(
        trade_date=trade_date,
        segment_code=segment_code,
        config_id_used=workflow.id,
        segment_status=SegmentStatus.PENDING,
        processes_json={},
    )
    session.add(row)
    await session.flush()
    logger.info(f"[OPS] segment={segment_code} trade_date={trade_date} | Segment row created")
    return row


@otel_trace
async def seed_from_workflow(
    session: AsyncSession,
    workflow: EdpProperties,
    trade_date: date,
) -> List[SegmentExecution]:
    """
    Bulk equivalent of get_or_create() for every segment in the workflow
    config. Not called by the orchestrator (which seeds lazily); kept for
    test setup and other bulk-seeding callers.
    """
    created: List[SegmentExecution] = []
    for seg_cfg in workflow.workflow_json.get("segments", []):
        code = seg_cfg["segment_code"]
        existed = await is_record_exists(session, trade_date, code)
        row = await get_or_create(session, workflow, trade_date, code)
        if not existed:
            created.append(row)
    return created


@otel_trace
async def seed_post_trade_processes(
    session: AsyncSession,
    workflow: EdpProperties,
    trade_date: date,
) -> List[SegmentExecution]:
    """
    Bulk equivalent of get_or_create() for the 5 T+1 post-trade processes.
    Missing "post_trade_processes" key falls back to the fixed
    POST_TRADE_ORDER; an explicit empty list means "seed none." Not called
    by the orchestrator (which seeds lazily); kept for test setup.
    """
    if "post_trade_processes" in workflow.workflow_json:
        proc_configs = workflow.workflow_json["post_trade_processes"]
    else:
        proc_configs = [{"process_code": code} for code in POST_TRADE_ORDER]

    created: List[SegmentExecution] = []
    for proc_cfg in proc_configs:
        code = proc_cfg.get("process_code", "")
        if code not in POST_TRADE_ORDER:
            logger.warning(
                f"[OPS] post_trade_processes config has unrecognized process_code={code!r} "
                f"for {trade_date} — skipping (must be one of {POST_TRADE_ORDER})"
            )
            continue

        existed = await is_record_exists(session, trade_date, code)
        row = await get_or_create(session, workflow, trade_date, code)
        if not existed:
            created.append(row)

    if created:
        logger.info(f"Seeded {len(created)} post-trade process rows for {trade_date}")
    return created


@otel_trace
async def has_processing_started(session: AsyncSession, trade_date: date) -> bool:
    """
    True if any segment_execution row for trade_date has left PENDING —
    i.e. billing is already underway. Used by the workflow upload endpoint
    to defer a same-day config change to trade_date + 1 instead of
    mutating a live day's windows/login_id (see api/workflow.py).
    """
    stmt = (
        select(SegmentExecution.id)
        .where(
            SegmentExecution.trade_date == trade_date,
            SegmentExecution.segment_status != SegmentStatus.PENDING,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


@otel_trace
async def is_record_exists(
    session: AsyncSession,
    trade_date: date,
    segment_code: str,
) -> bool:
    """True if a segment_execution row already exists for (trade_date, segment_code)."""
    return await get_one(session, trade_date, segment_code) is not None


def is_handled(row: SegmentExecution) -> bool:
    """True once a row has reached a terminal status (COMPLETED/SKIPPED/FAILED)."""
    return row.segment_status in _TERMINAL_STATUSES


# =============================================================================
# State transitions — the single place terminal transitions happen
# =============================================================================

@otel_trace
async def move_to_state(
    session: AsyncSession,
    row: SegmentExecution,
    new_status: SegmentStatus,
    category: Optional[str] = None,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """
    Move a row to a new status, updating terminal bookkeeping fields
    (phase/process/completed_at/category/reason), then fire a best-effort
    alert email on a genuine change into a terminal status.
    """
    now = now or now_ist()
    prev_status = row.segment_status
    row.segment_status = new_status

    if new_status in (SegmentStatus.COMPLETED, SegmentStatus.SKIPPED):
        row.current_phase = SegmentPhase.DONE
        row.current_process = None
    if new_status in _TERMINAL_STATUSES:
        # FAILED deliberately leaves current_phase/current_process untouched
        # (frozen where the pipeline broke) for diagnostics.
        row.completed_at = now
    if category is not None:
        row.skip_category = category
    if reason is not None:
        row.skip_reason = reason

    await session.flush()
    # updated_at has onupdate=func.now() (server-side) — after the UPDATE
    # above, SQLAlchemy expires it, and a later plain attribute read (e.g.
    # in serialize_segment()) would try a synchronous reload that crashes
    # under the async engine. Re-set it locally so it's never expired.
    row.updated_at = now
    logger.info(
        f"[STATE] segment={row.segment_code} | {prev_status.value} -> {new_status.value} "
        f"category={category} reason={reason}"
    )

    if prev_status != new_status and new_status in _TERMINAL_STATUSES:
        await _send_terminal_alert(row)


async def _send_terminal_alert(row: SegmentExecution) -> None:
    """Best-effort email alert — failures are logged, never raised."""
    try:
        from global_email_service import send_segment_alert
        from ..utils.serializers import serialize_segment

        payload = serialize_segment(row)
        await asyncio.to_thread(send_segment_alert, payload)
        logger.info(
            f"[ALERT] segment={row.segment_code} | Alert email sent for "
            f"status={row.segment_status.value}"
        )
    except Exception as exc:
        logger.error(
            f"[ALERT] segment={row.segment_code} | Failed to send alert email "
            f"(status={row.segment_status.value}): {exc}",
            exc_info=True,
        )


# =============================================================================
# Heartbeat
# =============================================================================

@otel_trace
async def touch_heartbeat(session: AsyncSession, row: SegmentExecution) -> None:
    """Update last_heartbeat_at."""
    ts = now_ist()
    row.last_heartbeat_at = ts
    await session.flush()
    logger.info(
        f"[HEARTBEAT] segment={row.segment_code} | Heartbeat updated "
        f"phase={row.current_phase.value if row.current_phase else 'N/A'} "
        f"at={ts.strftime('%H:%M:%S')}"
    )


# =============================================================================
# Operational control — retry / skip
# =============================================================================

@otel_trace
async def retry_segment(
    session: AsyncSession,
    trade_date: date,
    segment_code: str,
) -> Optional[SegmentExecution]:
    """
    Reset a FAILED or SKIPPED segment back to PENDING so the pipeline can retry it.

    SKIPPED is included because timeouts, CBOS SKIP responses, and agent
    restarts (e.g. a holiday flag that gets corrected later, or an
    ops-approved manual override) land a segment in SKIPPED, not FAILED —
    ops still needs a way to re-drive it without directly touching the DB.

    Clears: status, phase, process_id, error fields, processes_json.
    Returns None if segment not found or not in FAILED/SKIPPED status.
    """
    row = await get_one(session, trade_date, segment_code)
    if not row or row.segment_status not in (SegmentStatus.FAILED, SegmentStatus.SKIPPED):
        return None

    row.segment_status = SegmentStatus.PENDING
    row.current_phase = None
    row.current_process = None
    row.process_id = None
    row.process_id_reserved_at = None
    row.skip_category = None
    row.skip_reason = None
    row.started_at = None
    row.completed_at = None
    row.processes_json = {}
    await session.flush()
    logger.info(
        f"[OPS] segment={segment_code} trade_date={trade_date} | "
        f"Segment RETRIED — reset to PENDING (processes_json cleared)"
    )
    return row


@otel_trace
async def skip_segment_manually(
    session: AsyncSession,
    trade_date: date,
    segment_code: str,
    reason: str,
    skipped_by: str,
) -> Optional[SegmentExecution]:
    """
    Manually skip a PENDING or IN_PROGRESS segment.
    Useful when a segment was already processed outside the agent or must be bypassed.
    Returns None if segment not found or already COMPLETED/SKIPPED/FAILED.
    """
    row = await get_one(session, trade_date, segment_code)
    if not row or is_handled(row):
        return None

    await move_to_state(
        session, row, SegmentStatus.SKIPPED,
        category="MANUAL_SKIP",
        reason=f"Manually skipped by {skipped_by}: {reason}",
    )
    logger.info(
        f"[OPS] segment={segment_code} trade_date={trade_date} | "
        f"Segment manually SKIPPED by={skipped_by} reason={reason}"
    )
    return row


@otel_trace
async def get_day_summary(
    session: AsyncSession,
    trade_date: date,
) -> dict:
    """
    Aggregated summary for all segments on a given date.
    Used by GET /edp/status/{date}.
    """
    from ..utils.serializers import serialize_segment_summary

    rows = await get_all_for_date(session, trade_date)
    counts = {"pending": 0, "in_progress": 0, "completed": 0, "skipped": 0, "failed": 0}
    for r in rows:
        key = r.segment_status.value.lower()
        counts[key] = counts.get(key, 0) + 1

    return {
        "trade_date": trade_date.isoformat(),
        "total": len(rows),
        **counts,
        "segments": [serialize_segment_summary(r) for r in rows],
    }
