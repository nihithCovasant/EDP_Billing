"""
segment_execution table — CRUD, seeding, locking, heartbeat, and queries.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    EdpProperties,
    LockState,
    RuntimeHealth,
    SegmentExecution,
    SegmentPhase,
    SegmentStatus,
)
from ..utils.constants import (
    MTF_OPS_SEGMENT_CODE,
    MTF_OPS_SEGMENT_NAME,
    get_sequence_order,
)
from ..utils.datetime_utils import now_ist, parse_window_dt
from cams_otel_lib import Logger as logger, otel_trace

DEFAULT_LOCK_TTL = 300  # seconds


# =============================================================================
# Queries
# =============================================================================

@otel_trace
async def get_one(
    session: AsyncSession,
    trade_date: date,
    segment_code: str,
    domain: str = "EDP",
) -> Optional[SegmentExecution]:
    stmt = select(SegmentExecution).where(
        SegmentExecution.trade_date == trade_date,
        SegmentExecution.domain == domain,
        SegmentExecution.segment_code == segment_code,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@otel_trace
async def get_all_for_date(
    session: AsyncSession,
    trade_date: date,
    domain: str = "EDP",
) -> List[SegmentExecution]:
    """Return all segment rows for a date, ordered by the fixed SEGMENT_ORDER."""
    stmt = select(SegmentExecution).where(
        SegmentExecution.trade_date == trade_date,
        SegmentExecution.domain == domain,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    rows.sort(key=lambda r: get_sequence_order(r.segment_code))
    return rows


@otel_trace
async def get_in_progress(
    session: AsyncSession,
    trade_date: date,
    domain: str = "EDP",
) -> Optional[SegmentExecution]:
    """Return the segment currently IN_PROGRESS, if any."""
    stmt = select(SegmentExecution).where(
        SegmentExecution.trade_date == trade_date,
        SegmentExecution.domain == domain,
        SegmentExecution.segment_status == SegmentStatus.IN_PROGRESS,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# =============================================================================
# Seeding
# =============================================================================

@otel_trace
async def seed_from_workflow(
    session: AsyncSession,
    workflow: EdpProperties,
    trade_date: date,
    domain: str = "EDP",
) -> List[SegmentExecution]:
    """
    Create PENDING segment_execution rows from the active workflow config.

    Idempotent for rows that have already started (IN_PROGRESS/COMPLETED/
    SKIPPED/FAILED) — those are left untouched. But if a row is still
    PENDING (pipeline hasn't touched it yet) and a newer workflow config
    was uploaded (different content_hash) before it started, the row is
    reconciled in place to the latest config instead of silently running
    with stale name/window values forever.
    """
    tz = ZoneInfo(workflow.workflow_json.get("timezone", "Asia/Kolkata"))
    created: List[SegmentExecution] = []
    updated: List[SegmentExecution] = []

    for seg_cfg in workflow.workflow_json.get("segments", []):
        code = seg_cfg["segment_code"]
        existing = await get_one(session, trade_date, code, domain)
        if existing:
            if (
                existing.segment_status == SegmentStatus.PENDING
                and existing.config_hash_used != workflow.content_hash
            ):
                existing.segment_name = seg_cfg.get("segment_name", code)
                existing.window_start_at = parse_window_dt(
                    trade_date, seg_cfg["window_start"], False, tz
                )
                existing.window_end_at = parse_window_dt(
                    trade_date,
                    seg_cfg["window_end"],
                    seg_cfg.get("window_end_next_day", False),
                    tz,
                )
                existing.config_id_used = workflow.id
                existing.config_hash_used = workflow.content_hash
                updated.append(existing)
                logger.info(
                    f"[OPS] segment={code} trade_date={trade_date} | "
                    f"Segment row updated from new config (was PENDING)"
                )
            continue  # already seeded

        row = SegmentExecution(
            trade_date=trade_date,
            domain=domain,
            segment_code=code,
            segment_name=seg_cfg.get("segment_name", code),
            config_id_used=workflow.id,
            config_hash_used=workflow.content_hash,
            segment_status=SegmentStatus.PENDING,
            processes_json={},
            window_start_at=parse_window_dt(
                trade_date, seg_cfg["window_start"], False, tz
            ),
            window_end_at=parse_window_dt(
                trade_date,
                seg_cfg["window_end"],
                seg_cfg.get("window_end_next_day", False),
                tz,
            ),
        )
        session.add(row)
        created.append(row)

    if created or updated:
        await session.flush()
        logger.info(
            f"Seeded {len(created)} / updated {len(updated)} segment_execution "
            f"rows for {trade_date}"
        )
    return created


@otel_trace
async def seed_mtf_ops_segment(
    session: AsyncSession,
    workflow: EdpProperties,
    trade_date: date,
    domain: str = "EDP",
) -> Optional[SegmentExecution]:
    """
    Create the virtual MTFOPS segment_execution row that drives the
    post-segment MTF operations chain (Collateral Valuation -> Collateral
    Allocation -> Fund Transfer -> MTF Buy -> MTF Sell -> Weekly Auto
    Closure — v2 doc steps 12-24).

    Idempotent — returns None if already seeded for this date.

    Given the highest sequence order (see utils/constants.get_sequence_order),
    the normal sequential loop in orchestrator.run_wake_cycle() will not touch
    this row until every real trade segment has reached COMPLETED or SKIPPED
    — no special-casing of the sequencing logic is needed.
    """
    if await get_one(session, trade_date, MTF_OPS_SEGMENT_CODE, domain):
        return None

    tz = ZoneInfo(workflow.workflow_json.get("timezone", "Asia/Kolkata"))
    # No fixed window_start — purely gated by segment sequencing above.
    # Generous end-of-day deadline so a slow chain isn't cut off mid-way.
    window_end_at = datetime.combine(
        trade_date + timedelta(days=1), dtime(23, 59, 59), tzinfo=tz
    )

    row = SegmentExecution(
        trade_date=trade_date,
        domain=domain,
        segment_code=MTF_OPS_SEGMENT_CODE,
        segment_name=MTF_OPS_SEGMENT_NAME,
        config_id_used=workflow.id,
        config_hash_used=workflow.content_hash,
        segment_status=SegmentStatus.PENDING,
        processes_json={},
        window_start_at=None,
        window_end_at=window_end_at,
    )
    session.add(row)
    await session.flush()
    logger.info(f"Seeded virtual MTFOPS segment_execution row for {trade_date}")
    return row


# =============================================================================
# Lock management
# =============================================================================

@otel_trace
async def acquire_lock(
    session: AsyncSession,
    row: SegmentExecution,
    owner: str,
    ttl_seconds: int = DEFAULT_LOCK_TTL,
) -> bool:
    """
    Optimistic lock on a segment_execution row.

    Returns True  → lock acquired, safe to process.
    Returns False → lock held by another owner with a valid TTL.

    On TTL expiry, the stale lock is taken over and runtime_health is set
    to RECOVERED so ops can see that a crash/restart occurred.
    """
    now = now_ist()
    if row.lock_state == LockState.LOCKED:
        if row.lock_expires_at and now < row.lock_expires_at:
            logger.info(
                f"[LOCK] segment={row.segment_code} | Lock held by owner={row.lock_owner} "
                f"expires={row.lock_expires_at.isoformat()} — not acquired"
            )
            return False
        logger.warning(
            f"[LOCK] segment={row.segment_code} | Lock TTL expired "
            f"old_owner={row.lock_owner} expired_at={row.lock_expires_at} "
            f"→ taking over as owner={owner} (RECOVERED)"
        )
        row.runtime_health = RuntimeHealth.RECOVERED

    row.lock_state = LockState.LOCKED
    row.lock_owner = owner
    row.lock_acquired_at = now
    row.lock_expires_at = now + timedelta(seconds=ttl_seconds)
    await session.flush()
    logger.info(
        f"[LOCK] segment={row.segment_code} | Lock ACQUIRED by owner={owner} "
        f"ttl={ttl_seconds}s expires={row.lock_expires_at.isoformat()}"
    )
    return True


@otel_trace
async def release_lock(session: AsyncSession, row: SegmentExecution) -> None:
    logger.info(
        f"[LOCK] segment={row.segment_code} | Lock RELEASED by owner={row.lock_owner}"
    )
    row.lock_state = LockState.UNLOCKED
    row.lock_owner = None
    row.lock_acquired_at = None
    row.lock_expires_at = None
    await session.flush()


@otel_trace
async def recover_stale_locks(session: AsyncSession) -> int:
    """
    Called on agent startup.
    Releases any locks whose TTL has expired (e.g. after a crash/pod restart).
    Returns the number of locks recovered.
    """
    now = now_ist()
    stmt = select(SegmentExecution).where(
        SegmentExecution.lock_state == LockState.LOCKED,
        SegmentExecution.lock_expires_at < now,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        old_owner = row.lock_owner
        row.lock_state = LockState.UNLOCKED
        row.lock_owner = None
        row.runtime_health = RuntimeHealth.RECOVERED
        logger.warning(
            f"[LOCK] segment={row.segment_code} | Stale lock recovered on startup "
            f"old_owner={old_owner} expired_at={row.lock_expires_at}"
        )
    if rows:
        await session.flush()
    return len(rows)


# =============================================================================
# Heartbeat
# =============================================================================

@otel_trace
async def touch_heartbeat(session: AsyncSession, row: SegmentExecution) -> None:
    """Update last_heartbeat_at and mark runtime_health=ACTIVE."""
    ts = now_ist()
    row.last_heartbeat_at = ts
    row.runtime_health = RuntimeHealth.ACTIVE
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
    domain: str = "EDP",
) -> Optional[SegmentExecution]:
    """
    Reset a FAILED or SKIPPED segment back to PENDING so the pipeline can retry it.

    SKIPPED is included because timeouts and CBOS SKIP responses (e.g. a
    holiday flag that gets corrected later, or an ops-approved manual
    override) land a segment in SKIPPED, not FAILED — ops still needs a way
    to re-drive it without directly touching the DB.

    Clears: status, phase, process_id, lock, error fields, processes_json.
    Returns None if segment not found or not in FAILED/SKIPPED status.
    """
    row = await get_one(session, trade_date, segment_code, domain)
    if not row or row.segment_status not in (SegmentStatus.FAILED, SegmentStatus.SKIPPED):
        return None

    row.segment_status = SegmentStatus.PENDING
    row.current_phase = None
    row.current_process = None
    row.process_id = None
    row.process_id_reserved_at = None
    row.lock_state = LockState.UNLOCKED
    row.lock_owner = None
    row.lock_acquired_at = None
    row.lock_expires_at = None
    row.skip_category = None
    row.skip_reason = None
    row.started_at = None
    row.completed_at = None
    row.runtime_health = RuntimeHealth.ACTIVE
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
    domain: str = "EDP",
) -> Optional[SegmentExecution]:
    """
    Manually skip a PENDING or IN_PROGRESS segment.
    Useful when a segment was already processed outside the agent or must be bypassed.
    Returns None if segment not found or already COMPLETED/SKIPPED/FAILED.
    """
    row = await get_one(session, trade_date, segment_code, domain)
    if not row or row.segment_status in (
        SegmentStatus.COMPLETED, SegmentStatus.SKIPPED, SegmentStatus.FAILED
    ):
        return None

    now = now_ist()
    row.segment_status = SegmentStatus.SKIPPED
    row.skip_category = "MANUAL_SKIP"
    row.skip_reason = f"Manually skipped by {skipped_by}: {reason}"
    row.current_phase = SegmentPhase.DONE
    row.current_process = None
    row.completed_at = now
    row.lock_state = LockState.UNLOCKED
    row.lock_owner = None
    row.lock_acquired_at = None
    row.lock_expires_at = None
    await session.flush()
    logger.info(
        f"[OPS] segment={segment_code} trade_date={trade_date} | "
        f"Segment manually SKIPPED by={skipped_by} reason={reason}"
    )
    return row

@otel_trace
async def get_day_summary(
    session: AsyncSession,
    trade_date: date,
    domain: str = "EDP",
) -> dict:
    """
    Aggregated summary for all segments on a given date.
    Used by GET /edp/status/{date}.
    """
    from ..utils.serializers import serialize_segment_summary

    rows = await get_all_for_date(session, trade_date, domain)
    counts = {"pending": 0, "in_progress": 0, "completed": 0, "skipped": 0, "failed": 0}
    for r in rows:
        key = r.segment_status.value.lower()
        counts[key] = counts.get(key, 0) + 1

    return {
        "trade_date": trade_date.isoformat(),
        "domain": domain,
        "total": len(rows),
        **counts,
        "segments": [serialize_segment_summary(r) for r in rows],
    }
