"""
EDP Orchestrator — wake cycle coordinator.

Responsibilities:
  1. Determine the active trading date.
  2. Ensure a workflow config and seed segment rows exist.
  3. Iterate segments in sequence order, skipping past terminals.
  4. Acquire the per-segment lock, delegate to the pipeline executor,
     then release the lock and update the heartbeat.

All pipeline logic lives in pipeline.executor / pipeline.stages.
All DB operations live in repository.*
All helper utilities live in utils.*
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .config import EdpBootstrapConfig, build_default_workflow_json
from .database import get_session
from .models import SegmentPhase, SegmentStatus, RuntimeHealth
from . import repository
from .pipeline import advance_pipeline
from .utils.constants import MTF_OPS_SEGMENT_CODE
from .utils.datetime_utils import resolve_active_date, ensure_aware
from .utils.log_fmt import edp_log, seg_log
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger, otel_trace


class EdpOrchestrator:
    """
    Drives the daily EDP billing pipeline across all 8 trade segments, then
    the post-segment MTF operations chain.

    Segments run sequentially:
      EQ → DR → CUR → SL → NCDEX → MCX → NSECOM → MF → MTFOPS (virtual)
    Segment N cannot start until N-1 is COMPLETED or SKIPPED.

    MTFOPS is a virtual segment (see utils/constants.py) that only starts
    once all 8 real segments are done — it drives Collateral Valuation,
    Collateral Allocation, Fund Transfer, MTF Buy, MTF Sell, and Weekly Auto
    Closure (v2 doc steps 12-24).
    """

    def __init__(self, config: EdpBootstrapConfig, cbos: CbosClient):
        self.config = config
        self.cbos = cbos
        self._tz = ZoneInfo(config.timezone)
        # Set once per wake cycle in run_wake_cycle() and read by
        # _process_one_segment() for every segment in that cycle, instead of
        # being re-passed as arguments on each of the (possibly many) calls.
        self._cycle_active_date = None
        self._cycle_now: Optional[datetime] = None

    # -------------------------------------------------------------------------
    # Public entry point — called by loop.py on every wake interval
    # -------------------------------------------------------------------------

    @otel_trace
    async def run_wake_cycle(self) -> dict:
        now = datetime.now(self._tz)
        active_date = resolve_active_date(
            now, self.config.active_date_cutoff_hour, self.config.timezone
        )
        # Snapshot for this cycle — every segment processed below shares the
        # same active_date/now instead of each _process_one_segment() call
        # needing them re-passed as arguments.
        self._cycle_active_date = active_date
        self._cycle_now = now
        summary = {
            "active_date": active_date.isoformat(),
            "agent_state": "RUNNING",
            "segments_processed": 0,
            "segments_advanced": 0,
            "segments_blocked": 0,
            "segments_completed": 0,
            "segments_skipped": 0,
            "segments_failed": 0,
        }

        # ------ Check agent RUNNING / STOPPED state -------------------------
        async with get_session() as session:
            state = await repository.get_effective_state(session)
        summary["agent_state"] = state
        if state == "STOPPED":
            logger.info(edp_log("Agent is STOPPED — wake cycle skipped", date=active_date))
            return summary

        # ------ Recover any stale locks from previous crash -----------------
        async with get_session() as session:
            recovered = await repository.recover_stale_locks(session)
        if recovered:
            logger.warning(edp_log(
                "Stale locks recovered on startup",
                count=recovered,
                date=active_date,
            ))

        # ------ Ensure workflow config exists for today ----------------------
        # Ops does NOT need to upload a config every day — only when something
        # changes (segments, windows, login IDs, etc). If nothing was uploaded
        # specifically for today, we carry forward the most recently uploaded
        # config (from any earlier date) and keep using it as-is.
        async with get_session() as session:
            workflow = await repository.get_active(session, active_date)
            if not workflow:
                workflow = await repository.get_latest_effective(session, active_date)
                if workflow:
                    logger.info(edp_log(
                        "No new config uploaded for today — reusing last uploaded config",
                        date=active_date,
                        config_uploaded_for=workflow.trade_date,
                        config_id=workflow.id,
                        config_hash=workflow.content_hash[:12],
                    ))
            if not workflow:
                if self.config.default_segments:
                    default_wf = build_default_workflow_json(
                        self.config.default_segments,
                        timezone=self.config.timezone,
                    )
                    workflow, _ = await repository.upload(
                        session, active_date, default_wf, uploaded_by="agent-bootstrap"
                    )
                    logger.info(edp_log(
                        "Default workflow auto-seeded",
                        date=active_date,
                        segments=len(self.config.default_segments),
                    ))
                else:
                    logger.warning(edp_log(
                        "No workflow config and no defaults — skipping cycle",
                        date=active_date,
                    ))
                    return summary

        # ------ Seed PENDING segment rows from the workflow config ----------
        async with get_session() as session:
            created = await repository.seed_from_workflow(session, workflow, active_date)
        if created:
            logger.info(edp_log(
                "Segment rows seeded",
                date=active_date,
                count=len(created),
                segments=[r.segment_code for r in created],
            ))

        # ------ Seed the virtual MTFOPS segment (post-segment MTF chain) ----
        if self.config.mtf_ops_enabled:
            async with get_session() as session:
                mtf_row = await repository.seed_mtf_ops_segment(session, workflow, active_date)
            if mtf_row:
                logger.info(edp_log(
                    "Virtual MTFOPS segment seeded — runs after all real segments complete",
                    date=active_date,
                ))

        # ------ Fetch ordered segments and drive each one -------------------
        async with get_session() as session:
            segments = await repository.get_all_for_date(session, active_date)

        # ------ Mark stale heartbeats before processing ----------------------
        STALE_THRESHOLD = timedelta(minutes=10)
        async with get_session() as session:
            all_segs = await repository.get_all_for_date(session, active_date)
            for seg in all_segs:
                if (
                    seg.segment_status == SegmentStatus.IN_PROGRESS
                    and seg.last_heartbeat_at
                    and (now - ensure_aware(seg.last_heartbeat_at, self._tz)) > STALE_THRESHOLD
                    and seg.runtime_health != RuntimeHealth.STALE
                ):
                    seg.runtime_health = RuntimeHealth.STALE
                    logger.warning(seg_log(
                        seg.segment_code, active_date,
                        "Segment heartbeat STALE",
                        phase=seg.current_phase.value if seg.current_phase else None,
                        last_heartbeat=seg.last_heartbeat_at.isoformat()
                        if seg.last_heartbeat_at else None,
                        threshold=str(STALE_THRESHOLD),
                    ))
            await session.flush()

        # ------ Drive each segment in sequence ------------------------------
        for seg_row in segments:
            status = seg_row.segment_status

            if status in (SegmentStatus.COMPLETED, SegmentStatus.SKIPPED):
                continue

            if status == SegmentStatus.FAILED:
                logger.warning(seg_log(
                    seg_row.segment_code, active_date,
                    "Segment FAILED — halting sequential chain",
                    reason=seg_row.skip_reason,
                ))
                break

            summary["segments_processed"] += 1
            t0 = time.monotonic()
            outcome = await self._process_one_segment(seg_row.segment_code)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            _log_segment_outcome(seg_row.segment_code, active_date, outcome, elapsed_ms)
            summary[f"segments_{outcome}"] = summary.get(f"segments_{outcome}", 0) + 1

            # Stop the chain if this segment didn't finish
            if outcome not in ("completed", "skipped"):
                break

        return summary

    # -------------------------------------------------------------------------
    # Per-segment orchestration
    # -------------------------------------------------------------------------

    @otel_trace
    async def _process_one_segment(
        self,
        segment_code: str,
    ) -> str:
        """
        Lock → run pipeline executor → release lock.
        Returns: "completed"|"skipped"|"failed"|"advanced"|"blocked"

        active_date/now are read from the self._cycle_* snapshot taken once
        at the top of run_wake_cycle() — identical for every segment driven
        within that cycle.
        """
        active_date = self._cycle_active_date
        now = self._cycle_now
        async with get_session() as session:
            row = await repository.get_one(session, active_date, segment_code)
            if not row:
                logger.error(seg_log(segment_code, active_date, "Segment row not found in DB"))
                return "failed"

            workflow = await repository.get_active(session, active_date)
            if not workflow:
                # Same carry-forward fallback as run_wake_cycle — a config
                # uploaded on an earlier date is still the "active" one for
                # today until ops uploads a change.
                workflow = await repository.get_latest_effective(session, active_date)
            if not workflow:
                logger.error(seg_log(segment_code, active_date, "No active workflow found"))
                return "failed"

            is_mtf_ops = segment_code == MTF_OPS_SEGMENT_CODE
            if is_mtf_ops:
                # Virtual segment — not listed in workflow_json.segments.
                # GTG checks use config.cbos_login_id; the fixed G_LID login
                # used for the actual trigger calls is hardcoded in the CBOS
                # client (see utils/constants.py).
                login_id = self.config.cbos_login_id
            else:
                seg_cfg = _find_segment_cfg(workflow.workflow_json, segment_code)
                if not seg_cfg:
                    logger.error(seg_log(
                        segment_code, active_date,
                        "Segment code missing from workflow_json — cannot process",
                    ))
                    return "failed"
                login_id = seg_cfg.get("login_id", self.config.cbos_login_id)

            window_start = ensure_aware(row.window_start_at, self._tz)
            window_end = ensure_aware(row.window_end_at, self._tz)

            # Window not yet open
            if window_start and now < window_start:
                logger.info(seg_log(
                    segment_code, active_date,
                    "Segment window not yet open — skipping this cycle",
                    window_opens=window_start.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                ))
                return "blocked"

            # Window deadline missed (PENDING only)
            if (
                window_end
                and now > window_end
                and row.segment_status == SegmentStatus.PENDING
            ):
                logger.warning(seg_log(
                    segment_code, active_date,
                    "Segment window deadline passed without starting — SKIPPING, "
                    "moving on to the next segment in sequence",
                    deadline=window_end.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                ))
                row.segment_status = SegmentStatus.SKIPPED
                row.skip_category = "TIMEOUT"
                row.skip_reason = f"Past deadline {window_end.isoformat()}"
                row.current_phase = SegmentPhase.DONE
                row.completed_at = now
                await session.flush()
                return "skipped"

            # Move PENDING → IN_PROGRESS
            if row.segment_status == SegmentStatus.PENDING:
                acquired = await repository.acquire_lock(
                    session, row, self.config.agent_instance_id, self.config.lock_ttl_seconds
                )
                if not acquired:
                    logger.info(seg_log(segment_code, active_date, "Lock not acquired — blocked"))
                    return "blocked"
                row.segment_status = SegmentStatus.IN_PROGRESS
                row.started_at = now
                if is_mtf_ops:
                    row.current_phase = SegmentPhase.COLLATERAL_VALUATION
                    row.current_process = "CollateralValuation"
                else:
                    row.current_phase = SegmentPhase.HOLIDAY_CHECK
                    row.current_process = "BeginFileUpload"
                await session.flush()
                logger.info(seg_log(
                    segment_code, active_date,
                    "Segment STARTED",
                    started_at=now.strftime("%H:%M:%S %Z"),
                    window_start=window_start.strftime("%H:%M:%S %Z") if window_start else None,
                    window_end=window_end.strftime("%H:%M:%S %Z") if window_end else None,
                    first_phase=row.current_phase.value,
                ))

            elif row.segment_status == SegmentStatus.IN_PROGRESS:
                acquired = await repository.acquire_lock(
                    session, row, self.config.agent_instance_id, self.config.lock_ttl_seconds
                )
                if not acquired:
                    logger.info(seg_log(
                        segment_code, active_date,
                        "Lock not acquired (held by another instance) — blocked",
                        owner=row.lock_owner,
                    ))
                    return "blocked"
                logger.info(seg_log(
                    segment_code, active_date,
                    "Resuming IN_PROGRESS segment",
                    phase=row.current_phase.value if row.current_phase else None,
                    process=row.current_process,
                    pid=row.process_id,
                ))
            else:
                return "blocked"

            try:
                result = await advance_pipeline(
                    cbos=self.cbos,
                    row=row,
                    session=session,
                    login_id=login_id,
                    now=now,
                )
            finally:
                terminal = row.segment_status in (
                    SegmentStatus.COMPLETED, SegmentStatus.FAILED, SegmentStatus.SKIPPED
                )
                if not terminal:
                    await repository.touch_heartbeat(session, row)
                await repository.release_lock(session, row)

        return result


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _find_segment_cfg(workflow_json: dict, segment_code: str) -> dict | None:
    for seg in workflow_json.get("segments", []):
        if seg.get("segment_code") == segment_code:
            return seg
    return None


def _log_segment_outcome(
    segment_code: str,
    active_date,
    outcome: str,
    elapsed_ms: int,
) -> None:
    msg_map = {
        "completed": ("info",  "Segment COMPLETED"),
        "skipped":   ("info",  "Segment SKIPPED (holiday)"),
        "failed":    ("error", "Segment FAILED"),
        "advanced":  ("info",  "Segment advanced — will continue next cycle"),
        "blocked":   ("info",  "Segment blocked — waiting for CBOS or window"),
    }
    level, label = msg_map.get(outcome, ("info", f"Segment outcome={outcome}"))
    log_fn = getattr(logger, level)
    log_fn(seg_log(segment_code, active_date, label, elapsed_ms=elapsed_ms))
