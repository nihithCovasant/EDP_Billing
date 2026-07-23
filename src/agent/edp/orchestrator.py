"""
EDP Orchestrator — wake cycle coordinator.

Each cycle: ensure a workflow config exists, lazily create-if-missing a
segment_execution record per configured segment/process
(repository.get_or_create()), then drive every not-yet-handled() row
independently — no segment/process is gated on any other's status, only
on its own wall-clock window. Post-trade processes run the same way, in
their own pass (_process_post_trade_chain()).

Single-instance deployment: no pod-to-pod locking. An IN_PROGRESS row
resumes at its persisted current_state on restart — the TRIGGERING
pre-commit marker (state_machine.RealSegmentStateMachine /
PostTradeStateMachine) protects the CBOS trigger call itself from
double-firing.

All pipeline logic lives in state_machine.* (SegmentFactory ->
AbstractSegmentStateMachine subclasses), all DB operations in repository.*.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .config import EdpBootstrapConfig, build_default_workflow_json
from .database import get_session
from .models import SegmentState, SegmentStatus
from . import repository
from .state_machine import SegmentFactory
from .utils.constants import (
    STALE_HEARTBEAT_THRESHOLD,
    SEGMENT_ORDER,
    POST_TRADE_ORDER,
    POST_TRADE_GTG_PROCESS_NAME,
    POST_TRADE_FIRST_WINDOW_START,
    POST_TRADE_DEFAULT_WINDOW_END,
    NEXT_DAY_WINDOW_SEGMENTS,
)
from .utils.datetime_utils import resolve_active_date, ensure_aware, parse_window_dt
from .utils.log_fmt import edp_log, seg_log
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger, otel_trace


class EdpOrchestrator:
    """
    Drives the daily EDP billing pipeline across the 9 trade segments
    (CASH/EQ, F&O/DR, CD/CUR, SLB, NCDEX, NCDEXPHY, MCX, MCXPHY, NSECOM),
    then the 5 T+1 post-trade processes (COLVAL, COLALLOC, MTFFT, DMRPT,
    DMSTMT) —
    both orders fixed code constants; login_id/CBOS ProcessName/window
    resolved from the ops-uploaded workflow_json.
    """

    def __init__(self, config: EdpBootstrapConfig, cbos: CbosClient,
                 edpb: "EdpbClient | None" = None):
        self.config = config
        self.cbos = cbos
        # Injected like cbos; None -> resolved lazily so tests that swap the
        # process client via set_edpb_client() after construction still win.
        self._edpb = edpb
        self._tz = ZoneInfo(config.timezone)
        # Snapshot for the current wake cycle — shared by every segment
        # processed within it instead of re-passing as arguments.
        self._cycle_active_date = None
        self._cycle_now: Optional[datetime] = None
        # Segment codes the ACTIVE date's normal path drives this cycle - the
        # manual sweep must not double-drive those rows, but must pick up an
        # active-date row whose segment is absent from today's config.
        self._cycle_configured_codes: tuple[str, ...] = ()

    @property
    def edpb(self):
        from .edpb_client import get_edpb_client

        return self._edpb or get_edpb_client()

    # -------------------------------------------------------------------------
    # Public entry point — called by loop.py on every wake interval
    # -------------------------------------------------------------------------

    @otel_trace
    async def run_wake_cycle(self) -> dict:
        now = datetime.now(self._tz)
        active_date = resolve_active_date(
            now, self.config.active_date_cutoff_hour, self.config.timezone
        )
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

        # ------ Ensure workflow config exists for today ---------------------
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
                    ))
            if not workflow:
                if self.config.default_segments:
                    default_wf = build_default_workflow_json(
                        self.config.default_segments,
                        post_trade_processes=self.config.default_post_trade_processes or None,
                    )
                    workflow, _ = await repository.upload(
                        session, active_date, default_wf, uploaded_by="agent-bootstrap",
                        # "default" always points at whichever row was most
                        # recently auto-seeded (no explicit config existed
                        # for that day) — overwrite_version=True moves the
                        # name forward instead of raising on the 2nd+ day
                        # this ever fires (see move_version_name()).
                        version_name="default", overwrite_version=True,
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

        # ------ Lazily ensure a record exists for each configured segment ---
        configured_codes = [
            seg_cfg["segment_code"] for seg_cfg in workflow.workflow_json.get("segments", [])
        ]
        ordered_codes = [c for c in SEGMENT_ORDER if c in configured_codes]

        self._cycle_configured_codes = tuple(ordered_codes)
        segments = []
        for segment_code in ordered_codes:
            async with get_session() as session:
                existed = await repository.is_record_exists(session, active_date, segment_code)
                row = await repository.get_or_create(session, workflow, active_date, segment_code)
            if not existed:
                logger.info(edp_log(
                    "Segment row created", date=active_date, segment=segment_code,
                ))
            segments.append(row)

        # Log any stale heartbeats — diagnostic only, nothing persisted.
        for seg in segments:
            if (
                seg.segment_status == SegmentStatus.IN_PROGRESS
                and seg.last_heartbeat_at
                and (now - ensure_aware(seg.last_heartbeat_at, self._tz)) > STALE_HEARTBEAT_THRESHOLD
            ):
                logger.warning(seg_log(
                    seg.segment_code, active_date,
                    "Segment heartbeat STALE",
                    state=seg.current_state.value if seg.current_state else None,
                    last_heartbeat=seg.last_heartbeat_at.isoformat(),
                    threshold=str(STALE_HEARTBEAT_THRESHOLD),
                ))

        # ------ Drive each segment, independently ---------------------------
        # No segment is gated on another's status — every not-yet-handled
        # segment is attempted every cycle (see repository.is_handled()).
        for seg_row in segments:
            if repository.is_handled(seg_row):
                continue

            summary["segments_processed"] += 1
            t0 = time.monotonic()
            outcome = await self._process_one_segment(seg_row.segment_code)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            _log_segment_outcome(seg_row.segment_code, active_date, outcome, elapsed_ms)
            summary[f"segments_{outcome}"] = summary.get(f"segments_{outcome}", 0) + 1

        # ------ Manually (re)activated rows for OTHER dates ------------------
        # Backfills and past-day retries (wayfinder ticket 13): rows ops
        # marked via retry / POST /edp/run whose trade_date is not today's.
        await self._process_manually_activated(summary)

        # Drive the 5 T+1 post-trade processes, independent of the segments above.
        await self._process_post_trade_chain(summary)

        return summary

    async def _process_manually_activated(self, summary: dict) -> None:
        """Drive every non-terminal manually_activated row for past dates
        (bounded lookback), window gating bypassed. Terminal transitions
        clear the marker (repository.move_to_state), so a finished backfill
        drops out of this sweep on its own."""
        active_date = self._cycle_active_date
        min_date = active_date - timedelta(days=self.config.manual_activation_lookback_days)
        async with get_session() as session:
            rows = await repository.get_manually_activated_rows(session, min_date)

        for row in rows:
            # The normal path already drives active-date rows whose segment is
            # in today's config; everything else (past dates, or an
            # active-date row ops activated for a segment MISSING from
            # today's config) belongs to this sweep.
            if (
                row.trade_date == active_date
                and row.segment_code in self._cycle_configured_codes
            ):
                continue
            summary["manual_runs_processed"] = summary.get("manual_runs_processed", 0) + 1
            t0 = time.monotonic()
            outcome = await self._process_one_segment(
                row.segment_code, trade_date=row.trade_date, bypass_window=True,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _log_segment_outcome(row.segment_code, row.trade_date, outcome, elapsed_ms)
            summary[f"manual_runs_{outcome}"] = summary.get(f"manual_runs_{outcome}", 0) + 1

    # -------------------------------------------------------------------------
    # Per-segment orchestration
    # -------------------------------------------------------------------------

    @otel_trace
    async def _process_one_segment(
        self,
        segment_code: str,
        trade_date: date | None = None,
        bypass_window: bool = False,
    ) -> str:
        """
        Run the pipeline executor for one cycle's worth of progress on this
        segment. Returns: "completed"|"skipped"|"failed"|"advanced"|"blocked"

        trade_date defaults to the cycle's active date; a different date +
        bypass_window=True is the manual-activation path (backfills/past-day
        retries, wayfinder ticket 13): wall-clock windows are meaningless for
        a past day, so gating is skipped - loudly - and execute_handler gets
        no window_end deadline (ops is watching a manual run by definition).
        """
        active_date = trade_date or self._cycle_active_date
        now = self._cycle_now
        async with get_session() as session:
            row = await repository.get_one(session, active_date, segment_code)
            if not row:
                logger.error(seg_log(segment_code, active_date, "Segment row not found in DB"))
                return "failed"

            workflow = await repository.get_active(session, active_date)
            if not workflow:
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
            state_machine = SegmentFactory.get_segment_state_machine(segment_code)
            # Inject the saga dependencies (mirrors cbos being passed in).
            state_machine.edpb = self.edpb
            state_machine.runtime_config = self.config

            if bypass_window:
                logger.warning(seg_log(
                    segment_code, active_date,
                    "MANUAL ACTIVATION - window gating BYPASSED for this run "
                    "(ops-requested backfill/retry; no window deadline applies)",
                ))
                window_end = None

            # Window not yet open
            if not bypass_window and not state_machine.is_my_time_window(now, window_start):
                logger.info(seg_log(
                    segment_code, active_date,
                    "Segment window not yet open — skipping this cycle",
                    window_opens=window_start.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                ))
                return "blocked"

            # Window deadline missed (PENDING only) — a local timeout, not a
            # CBOS-driven skip signal, so this is FAILED/TIMEOUT, not SKIPPED.
            if (
                not bypass_window
                and state_machine.is_my_window_over(now, window_end)
                and row.segment_status == SegmentStatus.PENDING
            ):
                logger.warning(seg_log(
                    segment_code, active_date,
                    "Segment window deadline passed without starting — marking FAILED",
                    deadline=window_end.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                ))
                await repository.move_to_state(
                    session, row, SegmentStatus.FAILED,
                    category="TIMEOUT",
                    reason=f"Past deadline {window_end.isoformat()}",
                    now=now,
                )
                return "failed"

            # Move PENDING → IN_PROGRESS
            if row.segment_status == SegmentStatus.PENDING:
                row.segment_status = SegmentStatus.IN_PROGRESS
                row.started_at = now
                row.current_state = SegmentState.INIT
                row.current_process = "BeginFileUpload"
                await session.flush()
                logger.info(seg_log(
                    segment_code, active_date,
                    "Segment STARTED",
                    started_at=now.strftime("%H:%M:%S %Z"),
                    window_start=window_start.strftime("%H:%M:%S %Z") if window_start else None,
                    window_end=window_end.strftime("%H:%M:%S %Z") if window_end else None,
                    first_state=row.current_state.value,
                ))

            elif row.segment_status == SegmentStatus.IN_PROGRESS:
                logger.info(seg_log(
                    segment_code, active_date,
                    "Resuming IN_PROGRESS segment",
                    state=row.current_state.value if row.current_state else None,
                    process=row.current_process,
                    pid=row.process_id,
                ))
            else:
                return "blocked"

            try:
                result = await state_machine.execute_handler(
                    cbos=self.cbos,
                    row=row,
                    session=session,
                    login_id=login_id,
                    now=now,
                    window_end=window_end,
                )
            finally:
                if not repository.is_handled(row):
                    await repository.touch_heartbeat(session, row)

        return result

    # -------------------------------------------------------------------------
    # Post-trade (T+1) orchestration — 5 processes, independent of segments
    # -------------------------------------------------------------------------

    @otel_trace
    async def _process_post_trade_chain(self, summary: dict) -> None:
        """Lazily ensure a record exists per configured process, then drive
        every not-yet-handled one independently. Mutates `summary` in place."""
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

        if "post_trade_processes" in workflow.workflow_json:
            proc_configs = workflow.workflow_json["post_trade_processes"]
        else:
            proc_configs = [{"process_code": code} for code in POST_TRADE_ORDER]
        configured_codes = [pc.get("process_code", "") for pc in proc_configs]
        configured_codes = [c for c in configured_codes if c in POST_TRADE_ORDER]
        ordered_codes = [c for c in POST_TRADE_ORDER if c in configured_codes]

        post_trade_rows = []
        for process_code in ordered_codes:
            async with get_session() as session:
                existed = await repository.is_record_exists(session, active_date, process_code)
                row = await repository.get_or_create(session, workflow, active_date, process_code)
            if not existed:
                logger.info(edp_log(
                    "Post-trade process row created", date=active_date, process=process_code,
                ))
            post_trade_rows.append(row)

        # Every process is driven independently — not gated on siblings.
        for row in post_trade_rows:
            if repository.is_handled(row):
                continue

            summary["post_trade_processed"] += 1
            t0 = time.monotonic()
            outcome = await self._process_one_post_trade(row.segment_code)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            _log_segment_outcome(row.segment_code, active_date, outcome, elapsed_ms)
            summary[f"post_trade_{outcome}"] = summary.get(f"post_trade_{outcome}", 0) + 1

    @otel_trace
    async def _process_one_post_trade(self, segment_code: str) -> str:
        """
        One cycle's worth of progress on this post-trade process. Mirrors
        _process_one_segment(); login_id/gtg_process_name/window resolved
        from the active config, falling back to fixed constants otherwise.
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
            window_end = _resolve_post_trade_window_end(
                segment_code, workflow.workflow_json, active_date, self._tz
            )
            state_machine = SegmentFactory.get_segment_state_machine(segment_code)

            if not state_machine.is_my_time_window(now, window_start):
                logger.info(seg_log(
                    segment_code, active_date,
                    "Post-trade process window not yet open — skipping this cycle",
                    window_opens=window_start.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                ))
                return "blocked"

            # Window deadline missed (PENDING only) — mirrors
            # _process_one_segment()'s same check; without it a process
            # that never even started would sit PENDING forever past its
            # deadline instead of failing loudly. (No bypass here: manual
            # activation covers real segments only, not post-trade.)
            if (
                state_machine.is_my_window_over(now, window_end)
                and row.segment_status == SegmentStatus.PENDING
            ):
                logger.warning(seg_log(
                    segment_code, active_date,
                    "Post-trade process window deadline passed without starting — marking FAILED",
                    deadline=window_end.strftime("%H:%M:%S %Z"),
                    now=now.strftime("%H:%M:%S %Z"),
                ))
                await repository.move_to_state(
                    session, row, SegmentStatus.FAILED,
                    category="TIMEOUT",
                    reason=f"Past deadline {window_end.isoformat()}",
                    now=now,
                )
                return "failed"

            if row.segment_status == SegmentStatus.PENDING:
                row.segment_status = SegmentStatus.IN_PROGRESS
                row.started_at = now
                row.current_state = SegmentState.WAITING_FOR_GTG
                row.current_process = gtg_process_name
                await session.flush()
                logger.info(seg_log(
                    segment_code, active_date,
                    "Post-trade process STARTED",
                    started_at=now.strftime("%H:%M:%S %Z"),
                    window_opens=window_start.strftime("%H:%M:%S %Z") if window_start else None,
                    first_state=row.current_state.value,
                ))

            elif row.segment_status == SegmentStatus.IN_PROGRESS:
                logger.info(seg_log(
                    segment_code, active_date,
                    "Resuming IN_PROGRESS post-trade process",
                    state=row.current_state.value if row.current_state else None,
                    process=row.current_process,
                ))
            else:
                return "blocked"

            try:
                result = await state_machine.execute_handler(
                    cbos=self.cbos,
                    row=row,
                    session=session,
                    login_id=login_id,
                    now=now,
                    window_end=window_end,
                )
            finally:
                if not repository.is_handled(row):
                    await repository.touch_heartbeat(session, row)

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
    """Resolve (window_start, window_end) for a segment on demand — a pure
    function of (segment_code, workflow_json, trade_date, tz), so a config
    re-upload takes effect immediately.

    Two patterns, chosen by NEXT_DAY_WINDOW_SEGMENTS (a fixed regulatory
    set, e.g. MCX/MCXPHY/NSECOM run entirely T+1 morning):
      - Most segments run same-day-evening-into-next-morning: window_end
        only rolls onto trade_date+1 when it's chronologically at/before
        window_start on trade_date (e.g. window_start=17:00,
        window_end=06:00 crosses midnight) — derived from the actual times,
        never a blanket "always next day".
      - NEXT_DAY_WINDOW_SEGMENTS members have BOTH window_start and
        window_end on trade_date+1 — plain HH:MM strings can't otherwise
        distinguish that from a same-day-morning window (e.g.
        window_start=04:00 is ambiguous between "today" and "tomorrow"
        without this explicit signal; nothing here crosses midnight for
        the same-day/next-day check above to catch).

    Only the 5 post-trade processes are unconditionally T+1 regardless of
    segment_code; see _resolve_post_trade_window()."""
    seg_cfg = _find_segment_cfg(workflow_json, segment_code)
    if not seg_cfg:
        return None, None
    starts_next_day = segment_code in NEXT_DAY_WINDOW_SEGMENTS
    window_start = parse_window_dt(trade_date, seg_cfg["window_start"], starts_next_day, tz)
    window_end = parse_window_dt(trade_date, seg_cfg["window_end"], starts_next_day, tz)
    if window_end <= window_start:
        window_end = parse_window_dt(trade_date, seg_cfg["window_end"], True, tz)
    return window_start, window_end


def _post_trade_configs(workflow_json: dict) -> list[dict]:
    """The active config's post_trade_processes list, or the fixed legacy
    default (POST_TRADE_ORDER, no overrides) if the config predates this
    field entirely. An explicit EMPTY list means "seed none" and is kept
    as-is."""
    if "post_trade_processes" in workflow_json:
        return workflow_json["post_trade_processes"]
    return [{"process_code": code} for code in POST_TRADE_ORDER]


def _find_post_trade_cfg(workflow_json: dict, process_code: str) -> dict | None:
    for proc in _post_trade_configs(workflow_json):
        if proc.get("process_code") == process_code:
            return proc
    return None


def _resolve_post_trade_process_name(process_code: str, workflow_json: dict) -> str:
    """CBOS ProcessName for this process's GTG/confirm polls: explicit
    gtg_process_name > fixed default mapping > raw process_code."""
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
    Resolve the opening gate for a post-trade process, mirroring
    _resolve_window(). Every one of the 5 processes defaults to the fixed
    T+1 02:00 IST gate — none of them may start before then — unless that
    specific process has its own explicit window_start in workflow_json,
    which takes priority. Resolved independently per process, so one
    process's override has no effect on the others' default gate.

    Post-trade processing is T+1 by definition — a config can override the
    gate *time*, but not that it falls on trade_date+1, so this always
    resolves against the next calendar day.
    """
    proc_cfg = _find_post_trade_cfg(workflow_json, segment_code)
    if proc_cfg and proc_cfg.get("window_start"):
        return parse_window_dt(trade_date, proc_cfg["window_start"], True, tz)
    return parse_window_dt(trade_date, POST_TRADE_FIRST_WINDOW_START, True, tz)


def _resolve_post_trade_window_end(
    segment_code: str,
    workflow_json: dict,
    trade_date: date,
    tz: ZoneInfo,
) -> datetime:
    """
    Resolve the closing deadline for a post-trade process, mirroring
    _resolve_post_trade_window() for the opening gate. Every one of the 5
    processes defaults to the fixed T+1 06:00 IST deadline unless that
    specific process has its own explicit window_end in workflow_json.

    Without a real deadline here, a post-trade process CBOS never responds
    to would poll (BLOCKED) forever with no FAILED/TIMEOUT outcome and no
    alert ever firing — this closes that gap using the exact same
    is_my_window_over() check the 9 real segments already get.
    """
    proc_cfg = _find_post_trade_cfg(workflow_json, segment_code)
    if proc_cfg and proc_cfg.get("window_end"):
        return parse_window_dt(trade_date, proc_cfg["window_end"], True, tz)
    return parse_window_dt(trade_date, POST_TRADE_DEFAULT_WINDOW_END, True, tz)


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
