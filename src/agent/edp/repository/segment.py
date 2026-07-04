"""
segment_execution table — CRUD, seeding, locking, heartbeat, and queries.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    EdpProperties,
    SegmentExecution,
    SegmentPhase,
    SegmentStatus,
)
from ..utils.constants import (
    MTF_OPS_SEGMENT_CODE,
    get_sequence_order,
)
from ..utils.datetime_utils import now_ist
from ..utils.locking import (
    lock_expires_at,
    lock_owner,
    lock_state,
    set_locked,
    set_unlocked,
)
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
async def seed_from_workflow(
    session: AsyncSession,
    workflow: EdpProperties,
    trade_date: date,
) -> List[SegmentExecution]:
    """
    Create PENDING segment_execution rows from the active workflow config.

    Idempotent for rows that have already started (IN_PROGRESS/COMPLETED/
    SKIPPED/FAILED) — those are left untouched. Rows no longer carry
    segment_name/window_start_at/window_end_at (resolved on demand from
    segment_code / workflow_json instead — see utils/constants.py and
    orchestrator._resolve_window()), so there's nothing left to reconcile
    on a config re-upload except config_id_used/config_hash_used for audit.
    """
    created: List[SegmentExecution] = []

    for seg_cfg in workflow.workflow_json.get("segments", []):
        code = seg_cfg["segment_code"]
        existing = await get_one(session, trade_date, code)
        if existing:
            if (
                existing.segment_status == SegmentStatus.PENDING
                and existing.config_hash_used != workflow.content_hash
            ):
                existing.config_id_used = workflow.id
                existing.config_hash_used = workflow.content_hash
                logger.info(
                    f"[OPS] segment={code} trade_date={trade_date} | "
                    f"Segment row's config reference updated (was PENDING)"
                )
            continue  # already seeded

        row = SegmentExecution(
            trade_date=trade_date,
            segment_code=code,
            config_id_used=workflow.id,
            config_hash_used=workflow.content_hash,
            segment_status=SegmentStatus.PENDING,
            processes_json={},
        )
        session.add(row)
        created.append(row)

    if created:
        await session.flush()
        logger.info(f"Seeded {len(created)} segment_execution rows for {trade_date}")
    return created


@otel_trace
async def seed_mtf_ops_segment(
    session: AsyncSession,
    workflow: EdpProperties,
    trade_date: date,
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
    if await get_one(session, trade_date, MTF_OPS_SEGMENT_CODE):
        return None

    row = SegmentExecution(
        trade_date=trade_date,
        segment_code=MTF_OPS_SEGMENT_CODE,
        config_id_used=workflow.id,
        config_hash_used=workflow.content_hash,
        segment_status=SegmentStatus.PENDING,
        processes_json={},
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
    Returns False → lock held by another owner with a valid TTL, OR the
    lock is stale (TTL expired). Unlike before, a stale lock is NOT taken
    over and resumed here — recover_stale_locks() is solely responsible for
    resolving stale locks (by marking the segment SKIPPED), and it always
    runs earlier in the same wake cycle, before any segment reaches this
    call. Seeing a stale lock here would mean recover_stale_locks() hasn't
    caught up yet (a tight race) — safest is to just deny and let the next
    cycle's recovery sweep handle it.
    """
    now = now_ist()
    if lock_state(row) == "LOCKED":
        expires_at = lock_expires_at(row)
        if expires_at and now < expires_at:
            logger.info(
                f"[LOCK] segment={row.segment_code} | Lock held by owner={lock_owner(row)} "
                f"expires={expires_at.isoformat()} — not acquired"
            )
        else:
            logger.warning(
                f"[LOCK] segment={row.segment_code} | Lock stale (expired={expires_at}) "
                f"but not yet cleaned up by recover_stale_locks() — not acquiring"
            )
        return False

    expires_at = now + timedelta(seconds=ttl_seconds)
    set_locked(row, owner, now, expires_at)
    await session.flush()
    logger.info(
        f"[LOCK] segment={row.segment_code} | Lock ACQUIRED by owner={owner} "
        f"ttl={ttl_seconds}s expires={expires_at.isoformat()}"
    )
    return True


@otel_trace
async def release_lock(session: AsyncSession, row: SegmentExecution) -> None:
    logger.info(
        f"[LOCK] segment={row.segment_code} | Lock RELEASED by owner={lock_owner(row)}"
    )
    set_unlocked(row)
    await session.flush()


@otel_trace
async def recover_stale_locks(session: AsyncSession) -> int:
    """
    Called at the start of every wake cycle, before any segment is processed.

    A stale lock means the agent process died (crash/pod restart) while this
    segment was IN_PROGRESS and never reached release_lock(). Per policy, an
    interrupted segment is NOT resumed — it's marked SKIPPED (category
    AGENT_RESTART) and the day's chain moves on to the next segment, same as
    any other SKIP outcome (TIMEOUT / CBOS_SKIP / MANUAL_SKIP).

    Returns the number of segments skipped this way.
    """
    now = now_ist()
    stmt = select(SegmentExecution).where(
        SegmentExecution.segment_status == SegmentStatus.IN_PROGRESS,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    recovered = 0
    for row in rows:
        if lock_state(row) != "LOCKED":
            continue
        expires_at = lock_expires_at(row)
        if expires_at and now < expires_at:
            continue  # still validly locked — some other live instance owns it

        old_owner = lock_owner(row)
        stale_phase = row.current_phase.value if row.current_phase else "UNKNOWN"
        row.segment_status = SegmentStatus.SKIPPED
        row.skip_category = "AGENT_RESTART"
        row.skip_reason = (
            f"Interrupted mid-{stale_phase} — agent restarted/crashed "
            f"(stale lock held by owner={old_owner})"
        )
        row.current_phase = SegmentPhase.DONE
        row.current_process = None
        row.completed_at = now
        set_unlocked(row)
        recovered += 1
        logger.warning(
            f"[LOCK] segment={row.segment_code} | Stale lock found — SKIPPING segment "
            f"(was mid-{stale_phase}), old_owner={old_owner}"
        )
    if rows:
        await session.flush()
    return recovered


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

    Clears: status, phase, process_id, lock, error fields, processes_json.
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
    set_unlocked(row)
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
    set_unlocked(row)
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
