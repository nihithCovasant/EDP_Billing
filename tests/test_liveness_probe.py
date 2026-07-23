"""
The Kubernetes liveness probe (/health/live -> HealthChecker.is_alive())
used to be a hardcoded `return True` — incapable of detecting a wedged
EdpWakeLoop (e.g. an unresponsive CBOS call with no timeout blocking a
cycle forever), so the HTTP server would keep answering 200 indefinitely
while the entire billing pipeline silently stalled.

These are unit tests against EdpWakeLoop.liveness_check() and
HealthChecker.register_liveness_check()/is_alive() directly — no database,
no CBOS, no real wake loop thread — using time.monotonic() manipulation to
simulate "a cycle started N seconds ago and never finished".
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.agent.edp.config import EdpBootstrapConfig
from src.agent.edp.loop import EdpWakeLoop
from src.utils.health import HealthChecker


def _make_loop(wake_interval_seconds: int = 60) -> EdpWakeLoop:
    loop = EdpWakeLoop()
    loop._config = EdpBootstrapConfig(wake_interval_seconds=wake_interval_seconds)
    return loop


class _FakeTask:
    def done(self) -> bool:
        return False


async def test_liveness_healthy_before_any_cycle_has_run():
    loop = _make_loop()
    ok, reason = await loop.liveness_check()
    assert ok is True


async def test_liveness_healthy_when_never_started():
    loop = _make_loop()
    ok, reason = await loop.liveness_check()
    assert ok is True
    assert "starting up" in reason


async def test_liveness_healthy_when_stopped_intentionally():
    """DEF-017 fix: an intentional stop() (graceful shutdown) must not be
    confused with a crashed loop — both used to collapse into the same
    "not running" -> always-alive branch."""
    loop = _make_loop()
    loop._task = None
    loop._last_cycle_started_at = time.monotonic()
    loop._stop_event.set()
    ok, reason = await loop.liveness_check()
    assert ok is True
    assert "stopped intentionally" in reason


async def test_liveness_unhealthy_when_task_reference_gone_without_a_stop():
    """DEF-017 fix: previously `self._task is None` always reported alive,
    even if the loop had been running and its task reference vanished
    without stop() ever being called — now that's treated as dead."""
    loop = _make_loop()
    loop._task = None
    loop._last_cycle_started_at = time.monotonic()
    # _stop_event deliberately left unset — nobody asked for a shutdown.
    ok, reason = await loop.liveness_check()
    assert ok is False
    assert "without an intentional stop" in reason


async def test_liveness_unhealthy_when_task_completed_unexpectedly():
    """DEF-017 fix: a wake-loop task that finished on its own (crashed or
    otherwise exited) without an intentional stop() must fail liveness —
    previously `self._task.done()` always reported alive, forever hiding a
    dead pipeline behind a healthy-looking probe."""
    loop = _make_loop()

    async def _boom():
        raise RuntimeError("simulated crash")

    loop._task = asyncio.ensure_future(_boom())
    loop._last_cycle_started_at = time.monotonic()
    try:
        await loop._task
    except RuntimeError:
        pass
    ok, reason = await loop.liveness_check()
    assert ok is False
    assert "terminated unexpectedly" in reason


async def test_liveness_healthy_when_task_completed_after_intentional_stop():
    loop = _make_loop()

    async def _noop():
        return None

    loop._task = asyncio.ensure_future(_noop())
    loop._last_cycle_started_at = time.monotonic()
    loop._stop_event.set()
    await loop._task
    ok, reason = await loop.liveness_check()
    assert ok is True
    assert "stopped intentionally" in reason


async def test_liveness_healthy_shortly_after_a_cycle_starts():
    loop = _make_loop(wake_interval_seconds=60)
    loop._task = _FakeTask()
    loop._last_cycle_started_at = time.monotonic()  # just started
    ok, reason = await loop.liveness_check()
    assert ok is True


async def test_liveness_unhealthy_when_cycle_has_been_running_way_too_long():
    loop = _make_loop(wake_interval_seconds=60)
    loop._task = _FakeTask()
    # Simulate a cycle that "started" far longer ago than any reasonable
    # multiple of wake_interval_seconds (60s * 3 = 180s threshold).
    loop._last_cycle_started_at = time.monotonic() - 3600
    ok, reason = await loop.liveness_check()
    assert ok is False
    assert "wedged" in reason


async def test_liveness_uses_a_floor_threshold_for_short_intervals():
    """A tiny wake_interval_seconds (e.g. in tests) must not make the probe
    flap on ordinary jitter — _MIN_STALE_THRESHOLD_SECONDS floors it."""
    loop = _make_loop(wake_interval_seconds=1)
    loop._task = _FakeTask()
    loop._last_cycle_started_at = time.monotonic() - 30  # 30s, well under the 120s floor
    ok, reason = await loop.liveness_check()
    assert ok is True


async def test_health_checker_is_alive_fails_when_a_registered_check_fails():
    checker = HealthChecker()

    async def failing_check():
        return False, "simulated wedge"

    checker.register_liveness_check(failing_check)
    assert await checker.is_alive() is False


async def test_health_checker_is_alive_true_when_no_checks_registered():
    checker = HealthChecker()
    assert await checker.is_alive() is True


async def test_health_checker_is_alive_false_if_check_raises():
    checker = HealthChecker()

    async def raising_check():
        raise RuntimeError("boom")

    checker.register_liveness_check(raising_check)
    assert await checker.is_alive() is False


async def test_health_checker_integrates_real_wake_loop_liveness_check():
    """End-to-end wiring: a wedged EdpWakeLoop must flip the HealthChecker's
    is_alive() the same way __main__.py wires them together."""
    loop = _make_loop(wake_interval_seconds=60)
    loop._task = _FakeTask()
    loop._last_cycle_started_at = time.monotonic() - 3600

    checker = HealthChecker()
    checker.register_liveness_check(loop.liveness_check)
    assert await checker.is_alive() is False
