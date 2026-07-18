"""
24/7 EDP wake loop — sleeps N seconds between cycles.

Self-rescheduling asyncio.create_task() chain: each cycle schedules the
next one itself (see _cycle_wrapper()). Shutdown-safe via _stop_event
checks at every re-entry point + an atomic task handoff in stop(), so a
task can never be orphaned by a race with shutdown.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from .config import load_edp_config, EdpBootstrapConfig
from .database import close_database, init_database
from .orchestrator import EdpOrchestrator
from .utils.datetime_utils import now_ist
from .utils.log_fmt import edp_log
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger, otel_trace

# A cycle taking longer than this many multiples of wake_interval_seconds
# to even START is considered wedged (some await inside the previous cycle
# never returned) — see liveness_check().
_STALE_CYCLE_MULTIPLIER = 3
# Floor for the above, so a very short wake_interval_seconds (e.g. in tests)
# can't make the liveness check flap on ordinary cycle-to-cycle jitter.
_MIN_STALE_THRESHOLD_SECONDS = 120


class EdpWakeLoop:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._orchestrator: Optional[EdpOrchestrator] = None
        self._cycle_count: int = 0
        self._config: Optional[EdpBootstrapConfig] = None
        # Monotonic (not wall-clock) timestamp of the last cycle start — used
        # by liveness_check() to detect a wedged loop.
        self._last_cycle_started_at: Optional[float] = None
        # Wall-clock (IST) start/end timestamps of the last cycle — reported
        # by health_snapshot() for GET /edp/health; separate from the
        # monotonic field above, which exists purely for staleness math.
        self._last_cycle_started_at_wall: Optional[datetime] = None
        self._last_cycle_ended_at_wall: Optional[datetime] = None

    @otel_trace
    async def start(self) -> None:
        config = load_edp_config()
        logger.info(edp_log(
            "EDP startup: initializing database (running Alembic migrations "
            "if any are pending — first-time/fresh-DB runs can take a while)"
        ))
        t_db0 = time.monotonic()
        await init_database(config.database_url)
        logger.info(edp_log(
            "EDP startup: database ready",
            elapsed_ms=int((time.monotonic() - t_db0) * 1000),
        ))
        cbos = CbosClient(
            status_url=config.cbos_status_url,
            process_url=config.cbos_process_url,
            use_mock=config.cbos_use_mock,
        )
        self._orchestrator = EdpOrchestrator(config, cbos)
        self._config = config
        self._stop_event.clear()
        self._cycle_count = 0
        self._task = asyncio.create_task(self._cycle_wrapper(), name="edp-wake-loop")
        logger.info(edp_log(
            "Wake loop started",
            interval_s=config.wake_interval_seconds,
            mock_cbos=config.cbos_use_mock,
            status_url=config.cbos_status_url,
            process_url=config.cbos_process_url,
            instance=config.agent_instance_id,
        ))

    @otel_trace
    async def stop(self) -> None:
        self._stop_event.set()
        # Atomic capture so a task created concurrently with this call is
        # never dropped — it self-terminates on its own _stop_event check.
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await close_database()
        logger.info(edp_log(
            "Wake loop stopped",
            total_cycles=self._cycle_count,
        ))

    async def _cycle_wrapper(self) -> None:
        """Run one cycle, then reschedule itself unless stopped. Every
        re-entry checks _stop_event first, so a task created in a race
        with stop() self-terminates instead of running unmanaged."""
        if self._stop_event.is_set():
            return

        await self._run_one_cycle()

        if self._stop_event.is_set():
            return

        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self._config.wake_interval_seconds,
            )
            return  # stop_event was set while sleeping
        except asyncio.TimeoutError:
            pass  # normal case — time for the next cycle

        if self._stop_event.is_set():
            return

        self._task = asyncio.create_task(self._cycle_wrapper(), name="edp-wake-loop")

    async def _run_one_cycle(self) -> None:
        """Run exactly one wake cycle: orchestrator pass + START/END/ERROR logging."""
        self._last_cycle_started_at = time.monotonic()
        self._last_cycle_started_at_wall = now_ist()
        self._cycle_count += 1
        cycle_no = self._cycle_count
        t0 = time.monotonic()
        logger.info(edp_log(f"── Wake cycle #{cycle_no} START ──"))

        try:
            if self._orchestrator:
                summary = await self._orchestrator.run_wake_cycle()
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                logger.info(edp_log(
                    f"── Wake cycle #{cycle_no} END ──",
                    elapsed_ms=elapsed_ms,
                    date=summary.get("active_date"),
                    state=summary.get("agent_state"),
                    processed=summary.get("segments_processed", 0),
                    completed=summary.get("segments_completed", 0),
                    skipped=summary.get("segments_skipped", 0),
                    blocked=summary.get("segments_blocked", 0),
                    failed=summary.get("segments_failed", 0),
                ))
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.error(edp_log(
                f"── Wake cycle #{cycle_no} ERROR ──",
                elapsed_ms=elapsed_ms,
                error=str(exc),
            ), exc_info=True)
        finally:
            # Recorded even on error/exception -- an "ended" timestamp that
            # never advances is itself useful wedged-loop evidence.
            self._last_cycle_ended_at_wall = now_ist()

    @property
    def cbos(self) -> Optional[CbosClient]:
        """The orchestrator's CbosClient, if the loop has started — used by
        GET /edp/health's CBOS connectivity check (see __main__.py)."""
        return self._orchestrator.cbos if self._orchestrator else None

    def health_snapshot(self) -> Dict[str, Any]:
        """
        Billing-loop section of GET /edp/health (see __main__.py) — whether
        the loop is running at all, plus the last cycle's start/end wall-clock
        times, distinct from liveness_check()'s wedged-loop staleness math.
        """
        running = self._task is not None and not self._task.done()
        return {
            "running": running,
            "cycle_count": self._cycle_count,
            "last_cycle_started_at": (
                self._last_cycle_started_at_wall.isoformat()
                if self._last_cycle_started_at_wall else None
            ),
            "last_cycle_ended_at": (
                self._last_cycle_ended_at_wall.isoformat()
                if self._last_cycle_ended_at_wall else None
            ),
            "wake_interval_seconds": self._config.wake_interval_seconds if self._config else None,
        }

    async def liveness_check(self) -> Tuple[bool, str]:
        """
        Registered with the app's HealthChecker (see src/agent/__main__.py)
        as a liveness probe. Detects a wedged wake loop: if
        _last_cycle_started_at stops advancing for way longer than
        wake_interval_seconds, some await is blocking forever — only an
        external restart (Kubernetes acting on a failed liveness probe)
        can recover it.
        """
        if self._task is None or self._task.done():
            return True, "wake loop not running (stopped or never started)"
        if self._last_cycle_started_at is None or self._config is None:
            return True, "wake loop starting up, no cycle run yet"

        elapsed = time.monotonic() - self._last_cycle_started_at
        threshold = max(
            self._config.wake_interval_seconds * _STALE_CYCLE_MULTIPLIER,
            _MIN_STALE_THRESHOLD_SECONDS,
        )
        if elapsed > threshold:
            return False, (
                f"no wake cycle has started in {elapsed:.0f}s (expected every "
                f"~{self._config.wake_interval_seconds}s, threshold {threshold}s) "
                f"— wake loop appears wedged, cycle #{self._cycle_count} may be stuck"
            )
        return True, f"last cycle #{self._cycle_count} started {elapsed:.0f}s ago"
