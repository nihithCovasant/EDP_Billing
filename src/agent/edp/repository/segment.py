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

    Idempotent for rows that have already started (IN_PROGRESS/COMPLETED/
    SKIPPED/FAILED) — those are left untouched. Rows no longer carry
    segment_name/window_start_at/window_end_at (resolved on demand from
    segment_code / workflow_json instead — see utils/constants.py and
    orchestrator._resolve_window()), so there's nothing left to reconcile
    on a config re-upload except config_id_used for audit.
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
    Create PENDING segment_execution rows for the 5 T+1 post-trade processes
    from the active workflow config's `post_trade_processes` list — mirrors
    seed_from_workflow() for the 7 real segments.

    process_code must be one of the fixed POST_TRADE_ORDER codes: CBOS
    trigger-endpoint dispatch and the default GTG/confirm ProcessName
    mapping (see pipeline/post_trade_stages.py, utils/constants
    .POST_TRADE_GTG_PROCESS_NAME) are still fixed per code — there's no CBOS
    integration for an arbitrary 6th process — but login_id,
    gtg_process_name, and the opening window are now ops-controlled the
    same way segments' login_id/window are. Any unrecognized process_code
    in the config is skipped with a warning rather than crashing the wake
    cycle.

    Backward compatibility: a workflow_json that entirely predates this
    feature (no "post_trade_processes" key at all, e.g. an old row from
    before an upgrade) falls back to seeding the fixed 5 in POST_TRADE_ORDER
    unconditionally, same as the old hardcoded behavior, so existing
    deployments keep working unchanged until re-uploaded. An explicitly
    uploaded EMPTY list, by contrast, means ops wants no post-trade
    processes seeded at all for that config and is respected as such.

    Idempotent for rows that have already started — same config_id_used
    reconciliation behaviour as seed_from_workflow() for still-PENDING
    rows on a config re-upload.
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
    True if ANY segment_execution row for trade_date (one of the 7 real
    segments or the 5 T+1 post-trade processes) has left PENDING — i.e.
    billing is already underway for that date.

    Used by the workflow upload endpoint to protect an in-flight day from
    having its config changed out from under it mid-run: ops does not
    upload a config every day, and when they do upload a change while
    today's segments are already IN_PROGRESS/COMPLETED/SKIPPED/FAILED, that
    change is deferred to trade_date + 1 instead of mutating today's
    window_start/window_end/login_id live (see api/workflow.py). A date
    with every row still PENDING (windows not open yet, or nothing seeded
    at all) is NOT considered "started" — a same-day config change is safe
    right up until the first segment actually begins.
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
    Atomically acquire a lock on a segment_execution row via a single
    conditional UPDATE ... WHERE ... (not a read-then-write).

    Why this matters: reading row.lock_json in Python, branching, then
    writing it back via ORM attribute assignment + flush() is a classic
    check-then-act race. Two DB sessions — two pod replicas briefly
    overlapping during a rolling deploy, or two overlapping tasks in one
    process — that both read the same UNLOCKED row before either commits
    would BOTH believe they acquired the lock and BOTH proceed to call
    CBOS, defeating the entire point of locking. A single UPDATE with the
    "currently unlocked" condition baked into its own WHERE clause is
    atomic at the database level: row-level locking during the UPDATE
    itself (not read consistency) guarantees only one of two racing
    UPDATEs can ever match and change the row — the loser's WHERE clause
    simply stops matching the instant the winner's write lands, regardless
    of isolation level.

    The WHERE clause matches BOTH a row explicitly marked "UNLOCKED" and a
    freshly-seeded row that has never been locked at all (lock_json={},
    the column's default — no "state" key yet, so the JSON extraction
    below evaluates to SQL NULL rather than the string "UNLOCKED").

    Returns True  → lock acquired (row.lock_json refreshed in place).
    Returns False → lock held by another owner with a valid TTL, OR the
    lock is stale (TTL expired). Unlike before, a stale lock is NOT taken
    over and resumed here — recover_stale_locks() is solely responsible for
    resolving stale locks (by marking the segment SKIPPED, or — for a
    segment stuck mid-TRIGGERING — unlocking it for CBOS-checked recovery),
    and it always runs earlier in the same wake cycle, before any segment
    reaches this call. Seeing a stale lock here would mean
    recover_stale_locks() hasn't caught up yet (a tight race) — safest is
    to just deny and let the next cycle's recovery sweep handle it.
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

    A stale lock means the agent process died (crash/pod restart) while this
    segment was IN_PROGRESS and never reached release_lock(). Per policy, an
    interrupted segment is generally NOT resumed — it's marked SKIPPED
    (category AGENT_RESTART) and the day's chain moves on to the next
    segment, same as any other SKIP outcome (TIMEOUT / CBOS_SKIP /
    MANUAL_SKIP).

    TWO deliberate exceptions — a segment/process stuck with
    processes_json["trigger"]["status"] == "TRIGGERING" is never skipped
    here, for either pipeline:

      - Real segments (phase=TRIGGER): the crash happened right around the
        single getNewTradeProcess(trigger) call — we durably know an
        attempt was *intended* but not whether CBOS actually received it.
        Skipping outright would either strand a segment CBOS is already
        executing (our record says "skipped", CBOS is mid-run) or
        permanently discard the one signal needed to safely decide whether
        a retry is OK. Instead it's unlocked so the normal pipeline resumes
        it next cycle: handle_trigger() sees "TRIGGERING" and runs
        _recover_trigger(), which asks CBOS's Table2 for this exact
        PROCESSID and re-triggers ONLY if CBOS confirms it never received
        the original call — never if it already has (see
        tests/test_trigger_double_trigger_protection.py).

      - Post-trade processes (phase=TRIGGER_JOB): there is no Table2
        equivalent to check, so automatic recovery isn't possible at all —
        it's unlocked purely so handle_trigger_job() can immediately mark
        it FAILED with an explicit "needs manual CBOS verification" reason
        next cycle, instead of a generic, less actionable
        SKIPPED/AGENT_RESTART (see pipeline.post_trade_stages
        .handle_trigger_job).

    Returns the number of segments/processes affected (skipped OR
    unlocked-for-resume) this way.
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
