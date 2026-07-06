"""
edpb_segment_execution table — CRUD, seeding, locking, heartbeat, and queries.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    EdpProperties,
    LockState,
    SegmentExecution,
    SegmentPhase,
    SegmentStatus,
)
from ..utils.constants import get_sequence_order, POST_TRADE_ORDER
from ..utils.datetime_utils import now_ist
from ..utils.json_helpers import get_proc
from ..utils.locking import (
    lock_expires_at,
    lock_owner,
    lock_state,
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
    Idempotent for rows that have already started — those are left
    untouched except reconciling config_id_used for audit.
    """
    created: List[SegmentExecution] = []

    for seg_cfg in workflow.workflow_json.get("segments", []):
        code = seg_cfg["segment_code"]
        existing = await get_one(session, trade_date, code)
        if existing:
            if (
                existing.segment_status == SegmentStatus.PENDING
                and existing.config_id_used != workflow.id
            ):
                existing.config_id_used = workflow.id
                logger.info(
                    f"[OPS] segment={code} trade_date={trade_date} | "
                    f"Segment row's config reference updated (was PENDING)"
                )
            continue  # already seeded

        row = SegmentExecution(
            trade_date=trade_date,
            segment_code=code,
            config_id_used=workflow.id,
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
async def seed_post_trade_processes(
    session: AsyncSession,
    workflow: EdpProperties,
    trade_date: date,
) -> List[SegmentExecution]:
    """
    Create PENDING segment_execution rows for the 5 T+1 post-trade
    processes from workflow_json["post_trade_processes"] — mirrors
    seed_from_workflow(). process_code must be one of POST_TRADE_ORDER
    (unrecognized codes are skipped with a warning); a missing
    "post_trade_processes" key falls back to the fixed 5 for backward
    compatibility, while an explicit empty list means "seed none."
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

        existing = await get_one(session, trade_date, code)
        if existing:
            if (
                existing.segment_status == SegmentStatus.PENDING
                and existing.config_id_used != workflow.id
            ):
                existing.config_id_used = workflow.id
                logger.info(
                    f"[OPS] post_trade_process={code} trade_date={trade_date} | "
                    f"Process row's config reference updated (was PENDING)"
                )
            continue  # already seeded

        row = SegmentExecution(
            trade_date=trade_date,
            segment_code=code,
            config_id_used=workflow.id,
            segment_status=SegmentStatus.PENDING,
            processes_json={},
        )
        session.add(row)
        created.append(row)

    if created:
        await session.flush()
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
    Atomically acquire a lock via a single conditional UPDATE ... WHERE
    (not read-then-write), so two racing pods/tasks can't both believe
    they hold the lock — only one UPDATE can ever match the row.

    The WHERE clause matches a row marked "UNLOCKED" and a freshly-seeded
    row that's never been locked (lock_json={}, no "state" key yet).

    Returns True on success. Returns False if held by another owner with
    a valid TTL, or if stale — a stale lock is NOT taken over here;
    recover_stale_locks() (run earlier in the same cycle) owns resolving
    stale locks, so seeing one here means it hasn't caught up yet.
    """
    now = now_ist()
    expires_at = now + timedelta(seconds=ttl_seconds)
    new_lock = {
        "state": LockState.LOCKED.value,
        "owner": owner,
        "acquired_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    stmt = (
        update(SegmentExecution)
        .where(
            SegmentExecution.id == row.id,
            or_(
                SegmentExecution.lock_json["state"].as_string() == LockState.UNLOCKED.value,
                SegmentExecution.lock_json["state"].as_string().is_(None),
            ),
        )
        .values(lock_json=new_lock)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)

    if result.rowcount == 0:
        # Lost the race, or genuinely already locked — refresh from DB
        # (our in-memory `row` may be stale) purely to log which case it is.
        await session.refresh(row, attribute_names=["lock_json"])
        held_expires_at = lock_expires_at(row)
        if held_expires_at and now < held_expires_at:
            logger.info(
                f"[LOCK] segment={row.segment_code} | Lock held by owner={lock_owner(row)} "
                f"expires={held_expires_at.isoformat()} — not acquired"
            )
        else:
            logger.warning(
                f"[LOCK] segment={row.segment_code} | Lock stale (expired={held_expires_at}) "
                f"but not yet cleaned up by recover_stale_locks() — not acquiring"
            )
        return False

    row.lock_json = new_lock
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

    A stale lock means the agent crashed/restarted while IN_PROGRESS. Per
    policy an interrupted segment is generally marked SKIPPED (category
    AGENT_RESTART), not resumed.

    Two exceptions — a row stuck with trigger.status == "TRIGGERING" is
    never skipped here, since the crash happened right around the actual
    trigger call and we don't know if CBOS received it:
      - Real segments (phase=TRIGGER): unlocked so handle_trigger() runs
        _recover_trigger() next cycle, which checks CBOS's Table2 and only
        re-triggers if CBOS confirms it never received the original call.
      - Post-trade processes (phase=TRIGGER_JOB): no Table2 equivalent, so
        unlocked purely so handle_trigger_job() can mark it FAILED with a
        "needs manual CBOS verification" reason next cycle.

    Returns the number of rows affected (skipped or unlocked-for-resume).
    """
    now = now_ist()
    stmt = select(SegmentExecution).where(
        SegmentExecution.segment_status == SegmentStatus.IN_PROGRESS,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    affected = 0
    for row in rows:
        if lock_state(row) != "LOCKED":
            continue
        expires_at = lock_expires_at(row)
        if expires_at and now < expires_at:
            continue  # still validly locked — some other live instance owns it

        old_owner = lock_owner(row)
        stale_phase = row.current_phase.value if row.current_phase else "UNKNOWN"

        if (
            row.current_phase in (SegmentPhase.TRIGGER, SegmentPhase.TRIGGER_JOB)
            and get_proc(row, "trigger").get("status") == "TRIGGERING"
        ):
            set_unlocked(row)
            affected += 1
            logger.warning(
                f"[LOCK] segment={row.segment_code} | Stale lock found mid-TRIGGERING "
                f"(phase={stale_phase}, old_owner={old_owner}) — NOT skipping; unlocked "
                f"for recovery next cycle"
            )
            continue

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
        affected += 1
        logger.warning(
            f"[LOCK] segment={row.segment_code} | Stale lock found — SKIPPING segment "
            f"(was mid-{stale_phase}), old_owner={old_owner}"
        )
    if rows:
        await session.flush()
    return affected


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
