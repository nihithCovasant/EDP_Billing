"""
24/7 EDP wake loop — sleeps N seconds between cycles.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Tuple

from .config import load_edp_config, EdpBootstrapConfig
from .database import close_database, init_database
from .orchestrator import EdpOrchestrator
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
        self._task = asyncio.create_task(self._loop_forever(), name="edp-wake-loop")
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
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await close_database()
        logger.info(edp_log(
            "Wake loop stopped",
            total_cycles=self._cycle_count,
        ))

    async def _loop_forever(self) -> None:
        """
        One persistent Task for the process lifetime — created once in
        start() and never reassigned; runs wake cycles back-to-back until
        stop() cancels it. A single `while True`, not a chain of
        self-rescheduling tasks, so there's always exactly one Task to
        point at (asyncio.all_tasks(), debugger, incident review).

        The cancellable sleep (`asyncio.wait_for` on `_stop_event`) yields
        control every cycle so other coroutines keep running in between.
        """
        while not self._stop_event.is_set():
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

    async def _run_one_cycle(self) -> None:
        """Run exactly one wake cycle: orchestrator pass + START/END/ERROR logging."""
        self._last_cycle_started_at = time.monotonic()
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
