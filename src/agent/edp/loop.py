"""
24/7 EDP wake loop — sleeps N seconds between cycles.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from .config import load_edp_config, EdpBootstrapConfig
from .database import close_database, init_database
from .orchestrator import EdpOrchestrator
from .utils.log_fmt import edp_log
from src.tools.cbos_client import CbosClient
from cams_otel_lib import Logger as logger, otel_trace


class EdpWakeLoop:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._orchestrator: Optional[EdpOrchestrator] = None
        self._cycle_count: int = 0

    @otel_trace
    async def start(self) -> None:
        config = load_edp_config()
        await init_database(config.database_url)
        cbos = CbosClient(
            status_url=config.cbos_status_url,
            process_url=config.cbos_process_url,
            use_mock=config.cbos_use_mock,
        )
        self._orchestrator = EdpOrchestrator(config, cbos)
        self._stop_event.clear()
        self._cycle_count = 0
        self._task = asyncio.create_task(self._run_loop(config))
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

    async def _run_loop(self, config: EdpBootstrapConfig) -> None:
        while not self._stop_event.is_set():
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

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=config.wake_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                continue
