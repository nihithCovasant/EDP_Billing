"""
EDP Orchestrator — wake cycle coordinator.

Responsibilities:
  1. Determine the active trading date.
  2. Ensure a workflow config and seed segment rows exist.
  3. Iterate segments in sequence order, skipping past terminals.
  4. Acquire the per-segment lock, delegate to the pipeline executor,
     then release the lock and update the heartbeat.
  5. Independently seed and drive the 5 T+1 post-trade processes for the
     same active_date — NOT gated on the 7 segments' status (they run on
     their own fixed wall-clock window; see _process_post_trade_chain()).

All pipeline logic lives in pipeline.executor / pipeline.stages / pipeline.post_trade_stages.
All DB operations live in repository.*
All helper utilities live in utils.*
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .config import EdpBootstrapConfig, build_default_workflow_json
from .database import get_session
from .models import SegmentPhase, SegmentStatus
from . import repository
from .pipeline import advance_pipeline, POST_TRADE_PHASE_HANDLERS
from .utils.constants import (
    STALE_HEARTBEAT_THRESHOLD,
    SEGMENT_ORDER,
    POST_TRADE_ORDER,
    POST_TRADE_GTG_PROCESS_NAME,
    POST_TRADE_FIRST_WINDOW_START,
)
from .utils.datetime_utils import resolve_active_date, ensure_aware, parse_window_dt
from .utils.locking import lock_owner
from .utils.log_fmt import edp_log, seg_log
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger, otel_trace


class EdpOrchestrator:
    """
    Drives the daily EDP billing pipeline across all 7 trade segments, then
    the 5 T+1 post-trade processes.

    Segments run sequentially, in this fixed order (see utils/constants.py):
      CASH/EQ → F&O/DR → CD/CUR → SLBM/SL → MCX → NCDEX → MTF
    Segment N cannot start until N-1 is COMPLETED or SKIPPED. MTF is not
    special-cased — it runs through the exact same generic 7-step pipeline
    as every other segment.

    Independently of that chain, the 5 post-trade processes (COLVAL →
    COLALLOC → MTFFT → DMRPT → DMSTMT) are seeded and driven every cycle too
    — they are sequential among themselves (process N waits for N-1) but are
    NOT gated on the 7 segments' completion, only on their own wall-clock
    window (see _process_post_trade_chain()). Their process_code order is a
    fixed code constant (POST_TRADE_ORDER, same reasoning as SEGMENT_ORDER),
    but each process's login_id, CBOS ProcessName, and opening window are
    resolved from the same ops-uploaded workflow_json used for the 7
    segments (workflow_json["post_trade_processes"]).
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
            "post_trade_processed": 0,
            "post_trade_advanced": 0,
            "post_trade_blocked": 0,
            "post_trade_completed": 0,
            "post_trade_skipped": 0,
            "post_trade_failed": 0,
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
                        post_trade_processes=self.config.default_post_trade_processes or None,
                        timezone=self.config.timezone,
                    )
                    workflow, _ = await repository.upload(
                        session, active_date, default_wf, uploaded_by="agent-bootstrap"
                    )
                    logger.info(edp_log(
                        "Default workflow auto-seeded",
                        date=active_date,
                        segments=len(self.config.default_segments),
                        post_trade_processes=len(default_wf.get("post_trade_processes", [])),
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

        # ------ Fetch ordered segments and drive each one -------------------
        # get_all_for_date() returns EVERY segment_execution row for the date
        # — including the 5 post-trade pseudo-segments once they've been
        # seeded by a previous cycle's _process_post_trade_chain() — so this
        # loop must filter down to just the 7 real segments; post-trade rows
        # are driven separately below, independent of this loop entirely.
        async with get_session() as session:
            segments = await repository.get_all_for_date(session, active_date)
        segments = [s for s in segments if s.segment_code in SEGMENT_ORDER]

        # ------ Log any stale heartbeats — diagnostic only, nothing persisted.
        # (runtime_health used to be a column written here; it's now computed
        # live at read time in utils/serializers.py, so this is just a log
        # signal for ops and reuses the `segments` list already fetched
        # above instead of an extra DB round trip.)
        for seg in segments:
            if (
                seg.segment_status == SegmentStatus.IN_PROGRESS
                and seg.last_heartbeat_at
                and (now - ensure_aware(seg.last_heartbeat_at, self._tz)) > STALE_HEARTBEAT_THRESHOLD
            ):
                logger.warning(seg_log(
                    seg.segment_code, active_date,
                    "Segment heartbeat STALE",
                    phase=seg.current_phase.value if seg.current_phase else None,
                    last_heartbeat=seg.last_heartbeat_at.isoformat(),
                    threshold=str(STALE_HEARTBEAT_THRESHOLD),
                ))

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

        # ------ Drive the 5 T+1 post-trade processes — independent of the ---
        # 7 segments' status above (see class docstring); gated only by
        # their own fixed wall-clock window (Process 1 @ 02:30 IST T+1).
        await self._process_post_trade_chain(summary)

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

            seg_cfg = _find_segment_cfg(workflow.workflow_json, segment_code)
            if not seg_cfg:
                logger.error(seg_log(
                    segment_code, active_date,
                    "Segment code missing from workflow_json — cannot process",
                ))
                return "failed"
            login_id = seg_cfg.get("login_id", self.config.cbos_login_id)

            window_start, window_end = _resolve_window(
                segment_code, workflow.workflow_json, active_date, self._tz
            )

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
                        owner=lock_owner(row),
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
                    window_end=window_end,
                )
            finally:
                terminal = row.segment_status in (
                    SegmentStatus.COMPLETED, SegmentStatus.FAILED, SegmentStatus.SKIPPED
                )
                if not terminal:
                    await repository.touch_heartbeat(session, row)
                await repository.release_lock(session, row)

        return result

    # -------------------------------------------------------------------------
    # Post-trade (T+1) orchestration — 5 processes, independent of segments
    # -------------------------------------------------------------------------

    @otel_trace
    async def _process_post_trade_chain(self, summary: dict) -> None:
        """
        Seed (if needed) and drive the 5 T+1 post-trade processes for the
        current cycle's active_date, in fixed POST_TRADE_ORDER, halting the
        post-trade chain (not the whole wake cycle) on the first FAILED one.

        Deliberately NOT gated on the 7 real segments' status — see class
        docstring. Only Process 1 (COLVAL) has its own wall-clock window
        gate by default (see _resolve_post_trade_window()) — which
        process(es) actually carry a window, and each one's login_id /
        gtg_process_name, are resolved from the same active workflow config
        used for the 7 segments (workflow_json["post_trade_processes"]);
        mutates `summary` in place with post_trade_* counters.
        """
        active_date = self._cycle_active_date

        async with get_session() as session:
            workflow = await repository.get_active(session, active_date)
            if not workflow:
                workflow = await repository.get_latest_effective(session, active_date)
            if not workflow:
                logger.warning(edp_log(
                    "No workflow config available — cannot seed post-trade processes this cycle",
                    date=active_date,
                ))
                return
            created = await repository.seed_post_trade_processes(session, workflow, active_date)
        if created:
            logger.info(edp_log(
                "Post-trade process rows seeded",
                date=active_date,
                count=len(created),
                processes=[r.segment_code for r in created],
            ))

        async with get_session() as session:
            all_rows = await repository.get_all_for_date(session, active_date)
        post_trade_rows = [r for r in all_rows if r.segment_code in POST_TRADE_ORDER]
        post_trade_rows.sort(key=lambda r: POST_TRADE_ORDER.index(r.segment_code))

        for row in post_trade_rows:
            status = row.segment_status

            if status in (SegmentStatus.COMPLETED, SegmentStatus.SKIPPED):
                continue

            if status == SegmentStatus.FAILED:
                logger.warning(seg_log(
                    row.segment_code, active_date,
                    "Post-trade process FAILED — halting remaining post-trade chain",
                    reason=row.skip_reason,
                ))
                break

            summary["post_trade_processed"] += 1
            t0 = time.monotonic()
            outcome = await self._process_one_post_trade(row.segment_code)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            _log_segment_outcome(row.segment_code, active_date, outcome, elapsed_ms)
            summary[f"post_trade_{outcome}"] = summary.get(f"post_trade_{outcome}", 0) + 1

            if outcome not in ("completed", "skipped"):
                break

    @otel_trace
    async def _process_one_post_trade(self, segment_code: str) -> str:
        """
        Lock → run pipeline executor (post-trade phase handlers) → release lock.
        Returns: "completed"|"skipped"|"failed"|"advanced"|"blocked"

        Mirrors _process_one_segment() — login_id, gtg_process_name, and the
        opening window are resolved from the same active workflow config's
        `post_trade_processes` list (falling back to bootstrap config /
        fixed code constants for anything a config entry doesn't specify,
        or for a legacy config with no such list at all — see
        _find_post_trade_cfg() / _resolve_post_trade_window()).
        """
        active_date = self._cycle_active_date
        now = self._cycle_now
        async with get_session() as session:
            row = await repository.get_one(session, active_date, segment_code)
            if not row:
                logger.error(seg_log(segment_code, active_date, "Post-trade process row not found in DB"))
                return "failed"

            workflow = await repository.get_active(session, active_date)
            if not workflow:
                # Same carry-forward fallback as _process_one_segment.
                workflow = await repository.get_latest_effective(session, active_date)
            if not workflow:
                logger.error(seg_log(segment_code, active_date, "No active workflow found for post-trade process"))
                return "failed"

            proc_cfg = _find_post_trade_cfg(workflow.workflow_json, segment_code)
            login_id = (proc_cfg or {}).get("login_id") or self.config.post_trade_login_id
            gtg_process_name = _resolve_post_trade_process_name(segment_code, workflow.workflow_json)
            window_start = _resolve_post_trade_window(
                segment_code, workflow.workflow_json, active_date, self._tz
            )

            if window_start and now < window_start:
                logger.info(seg_log(
                    segment_code, active_date,
                    "Post-trade process window not yet open — skipping this cycle",
                    window_opens=window_start.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                ))
                return "blocked"

            if row.segment_status == SegmentStatus.PENDING:
                acquired = await repository.acquire_lock(
                    session, row, self.config.agent_instance_id, self.config.lock_ttl_seconds
                )
                if not acquired:
                    logger.info(seg_log(segment_code, active_date, "Lock not acquired — blocked"))
                    return "blocked"
                row.segment_status = SegmentStatus.IN_PROGRESS
                row.started_at = now
                row.current_phase = SegmentPhase.AWAIT_GTG
                row.current_process = gtg_process_name
                await session.flush()
                logger.info(seg_log(
                    segment_code, active_date,
                    "Post-trade process STARTED",
                    started_at=now.strftime("%H:%M:%S %Z"),
                    window_opens=window_start.strftime("%H:%M:%S %Z") if window_start else None,
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
                        owner=lock_owner(row),
                    ))
                    return "blocked"
                logger.info(seg_log(
                    segment_code, active_date,
                    "Resuming IN_PROGRESS post-trade process",
                    phase=row.current_phase.value if row.current_phase else None,
                    process=row.current_process,
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
                    window_end=None,
                    phase_handlers=POST_TRADE_PHASE_HANDLERS,
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


def _resolve_window(
    segment_code: str,
    workflow_json: dict,
    trade_date: date,
    tz: ZoneInfo,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Resolve (window_start, window_end) for a segment on demand.

    This used to be two columns (window_start_at/window_end_at) computed
    once at seed time; they were a pure function of
    (segment_code, workflow_json, trade_date, tz) the whole time, so
    resolving them here means a config re-upload takes effect immediately
    instead of only for segments that haven't been seeded yet.
    """
    seg_cfg = _find_segment_cfg(workflow_json, segment_code)
    if not seg_cfg:
        return None, None
    window_start = parse_window_dt(trade_date, seg_cfg["window_start"], False, tz)
    window_end = parse_window_dt(
        trade_date, seg_cfg["window_end"], seg_cfg.get("window_end_next_day", False), tz
    )
    return window_start, window_end


def _post_trade_configs(workflow_json: dict) -> list[dict]:
    """
    The active config's post_trade_processes list, or the fixed legacy
    default (POST_TRADE_ORDER order, no per-process login_id/window
    override) if the config predates this becoming ops-configurable at all
    — distinct from an explicitly-uploaded EMPTY list, which means ops
    wants no post-trade processes seeded/resolved for that config.
    """
    if "post_trade_processes" in workflow_json:
        return workflow_json["post_trade_processes"]
    return [{"process_code": code} for code in POST_TRADE_ORDER]


def _find_post_trade_cfg(workflow_json: dict, process_code: str) -> dict | None:
    for proc in _post_trade_configs(workflow_json):
        if proc.get("process_code") == process_code:
            return proc
    return None


def _resolve_post_trade_process_name(process_code: str, workflow_json: dict) -> str:
    """
    CBOS file_process_status ProcessName used for this process's GTG/confirm
    polls — an explicit `gtg_process_name` in the config entry wins, else
    falls back to the fixed default mapping (utils/constants
    .POST_TRADE_GTG_PROCESS_NAME), else the raw process_code itself as a
    last resort so an unmapped code can't crash the cycle.
    """
    proc_cfg = _find_post_trade_cfg(workflow_json, process_code)
    if proc_cfg and proc_cfg.get("gtg_process_name"):
        return proc_cfg["gtg_process_name"]
    return POST_TRADE_GTG_PROCESS_NAME.get(process_code, process_code)


def _resolve_post_trade_window(
    segment_code: str,
    workflow_json: dict,
    trade_date: date,
    tz: ZoneInfo,
) -> Optional[datetime]:
    """
    Resolve the opening gate (if any) for a post-trade process from the
    active config's post_trade_processes list — mirrors _resolve_window()
    for the 7 real segments. Per spec, only Process 1 (COLVAL) has a
    window_start ("T+1, 2:30am-6am"); the remaining 4 simply start as soon
    as the previous one in the chain completes. Which process(es) actually
    carry a window is now entirely config-driven rather than hardcoded to
    POST_TRADE_ORDER[0] — build_default_workflow_json() still defaults to
    putting it only on the first one, matching the original spec, but ops
    can move/add/remove it via a re-upload.

    Falls back to the fixed 02:30 IST T+1 gate on POST_TRADE_ORDER[0] only
    when NO process in the config specifies a window_start at all — i.e.
    either a legacy config that predates this feature, or a new config that
    simply didn't bother configuring any window. If ops explicitly
    configures a window_start anywhere in the list (even on a different
    process), that explicit choice is trusted as-is and no default is
    silently added elsewhere.
    """
    proc_cfg = _find_post_trade_cfg(workflow_json, segment_code)
    if proc_cfg and proc_cfg.get("window_start"):
        return parse_window_dt(
            trade_date, proc_cfg["window_start"], proc_cfg.get("window_start_next_day", True), tz
        )
    if segment_code == POST_TRADE_ORDER[0] and not any(
        p.get("window_start") for p in _post_trade_configs(workflow_json)
    ):
        return parse_window_dt(trade_date, POST_TRADE_FIRST_WINDOW_START, True, tz)
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
