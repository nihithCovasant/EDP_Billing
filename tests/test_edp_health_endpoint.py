"""
GET /edp/health -- reports billing-loop cycle start/stop times, live DB
connectivity, CBOS reachability, and the last alert email attempt; fails
with 503 if the billing loop, DB, or CBOS aren't healthy (see
src/agent/__main__.py::edp_health_check()).

Unit-level tests exercise each component in isolation (EdpWakeLoop.
health_snapshot(), CbosClient.check_connectivity(), database.
check_connectivity(), alert_health.py) without a real server; a couple of
end-to-end tests hit the actual endpoint via TestClient with the real
in-memory/mocked components wired up exactly like __main__.py does.
"""

from __future__ import annotations

import time

import pytest

from src.agent.edp.alert_health import get_alert_health, record_alert_attempt
from src.agent.edp.config import EdpBootstrapConfig
from src.agent.edp.loop import EdpWakeLoop
from src.tools.cbos_client import CbosClient


# =============================================================================
# EdpWakeLoop.health_snapshot()
# =============================================================================

class _FakeTask:
    def __init__(self, is_done: bool = False):
        self._is_done = is_done

    def done(self) -> bool:
        return self._is_done


def test_health_snapshot_reports_not_running_before_start():
    loop = EdpWakeLoop()
    snap = loop.health_snapshot()
    assert snap["running"] is False
    assert snap["cycle_count"] == 0
    assert snap["last_cycle_started_at"] is None
    assert snap["last_cycle_ended_at"] is None


def test_health_snapshot_reports_running_and_cycle_times_after_a_cycle():
    loop = EdpWakeLoop()
    loop._config = EdpBootstrapConfig(wake_interval_seconds=5)
    loop._task = _FakeTask(is_done=False)
    loop._cycle_count = 3

    from src.agent.edp.utils.datetime_utils import now_ist
    started = now_ist()
    loop._last_cycle_started_at_wall = started
    loop._last_cycle_ended_at_wall = now_ist()

    snap = loop.health_snapshot()
    assert snap["running"] is True
    assert snap["cycle_count"] == 3
    assert snap["last_cycle_started_at"] == started.isoformat()
    assert snap["last_cycle_ended_at"] is not None
    assert snap["wake_interval_seconds"] == 5


def test_health_snapshot_reports_not_running_when_task_is_done():
    loop = EdpWakeLoop()
    loop._task = _FakeTask(is_done=True)
    assert loop.health_snapshot()["running"] is False


def test_cbos_property_is_none_before_the_loop_has_started():
    loop = EdpWakeLoop()
    assert loop.cbos is None


# =============================================================================
# CbosClient.check_connectivity()
# =============================================================================

async def test_cbos_connectivity_mock_mode_always_reports_mock_and_ok():
    client = CbosClient(status_url="http://fake-status", process_url="http://fake-process", use_mock=True)
    result = await client.check_connectivity()
    assert result["status"] == "mock"
    assert result["status_url"]["ok"] is True
    assert result["process_url"]["ok"] is True


async def test_cbos_connectivity_real_mode_reports_error_for_unreachable_urls():
    # Ports deliberately unused/closed -- guaranteed connection failure, no
    # real network dependency for the test.
    client = CbosClient(
        status_url="http://127.0.0.1:1", process_url="http://127.0.0.1:2", use_mock=False,
    )
    result = await client.check_connectivity()
    assert result["status"] == "error"
    assert result["status_url"]["ok"] is False
    assert result["process_url"]["ok"] is False
    assert "error" in result["status_url"]


# =============================================================================
# database.check_connectivity()
# =============================================================================

async def test_database_connectivity_reports_error_when_not_initialized(monkeypatch):
    from src.agent.edp import database as db_module

    monkeypatch.setattr(db_module, "_session_factory", None)
    result = await db_module.check_connectivity()
    assert result["status"] == "error"
    assert "not initialized" in result["error"].lower()


async def test_database_connectivity_reports_ok_against_the_real_test_db():
    """wire_orchestrator_database (autouse, see conftest.py) already points
    database._session_factory at a real (test) database, so this genuinely
    exercises SELECT 1 without any extra setup."""
    from src.agent.edp import database as db_module

    result = await db_module.check_connectivity()
    assert result["status"] == "ok"
    assert result["latency_ms"] >= 0


# =============================================================================
# alert_health.py
# =============================================================================

def test_alert_health_reports_all_none_before_any_alert():
    import src.agent.edp.alert_health as alert_health_module

    alert_health_module._last_attempt_at = None
    alert_health_module._last_success_at = None
    alert_health_module._last_failure_at = None
    alert_health_module._last_error = None

    health = get_alert_health()
    assert health == {
        "last_attempt_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "last_error": None,
    }


def test_alert_health_records_a_successful_attempt():
    record_alert_attempt(success=True)
    health = get_alert_health()
    assert health["last_attempt_at"] is not None
    assert health["last_success_at"] is not None
    assert health["last_failure_at"] is None
    assert health["last_error"] is None


def test_alert_health_records_a_failed_attempt_with_its_error():
    record_alert_attempt(success=False, error="SMTP timeout")
    health = get_alert_health()
    assert health["last_attempt_at"] is not None
    assert health["last_failure_at"] is not None
    assert health["last_error"] == "SMTP timeout"


def test_alert_health_a_later_success_clears_the_previous_error():
    record_alert_attempt(success=False, error="boom")
    record_alert_attempt(success=True)
    health = get_alert_health()
    assert health["last_error"] is None
    assert health["last_success_at"] is not None


# =============================================================================
# End-to-end: GET /edp/health via the real app (loop disabled — no real
# CBOS/DB startup needed, mirrors how the other __main__.py tests do it).
# =============================================================================

@pytest.fixture()
def edp_health_client(monkeypatch):
    monkeypatch.setenv("EDP_LOOP_ENABLED", "false")
    from fastapi.testclient import TestClient
    from src.agent.__main__ import build_app

    app = build_app()
    with TestClient(app) as c:
        yield c


def test_edp_health_endpoint_returns_503_when_loop_is_disabled(edp_health_client):
    """With EDP_LOOP_ENABLED=false the wake loop never starts, so
    billing_loop.running is False -- must fail the overall check per the
    "if CBOS/DB/billing loop are not running, health check should fail"
    requirement, even though this is an intentional config choice rather
    than a crash."""
    resp = edp_health_client.get("/edp/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["billing_loop"]["running"] is False
    assert body["billing_loop"]["enabled"] is False
    assert "database" in body
    assert "cbos" in body
    assert "alerts" in body


def test_edp_health_endpoint_response_shape_has_all_required_sections(edp_health_client):
    resp = edp_health_client.get("/edp/health")
    body = resp.json()
    assert set(body.keys()) >= {"status", "checked_at", "billing_loop", "database", "cbos", "alerts"}
    assert set(body["billing_loop"].keys()) >= {
        "running", "cycle_count", "last_cycle_started_at", "last_cycle_ended_at",
        "wake_interval_seconds", "enabled", "alive", "alive_reason",
    }
    assert set(body["alerts"].keys()) == {
        "last_attempt_at", "last_success_at", "last_failure_at", "last_error",
    }
