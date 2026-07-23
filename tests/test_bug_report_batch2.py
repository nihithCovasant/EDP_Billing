"""
Regression tests for the second batch of MOFSL_EDP_Billing_Agent_Bug_Report
findings fixed together: DEF-007, DEF-011, DEF-013, DEF-014, DEF-015,
DEF-016, DEF-017 (see tests/test_liveness_probe.py), DEF-018.
"""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest
from fastapi import FastAPI

from src.agent.edp.api.audit import router as audit_router
from src.agent.edp.api.status import router as status_router
from src.agent.edp.models import SegmentExecution, SegmentState, SegmentStatus
from src.tools.simple_test_tool import simple_calculator


# =============================================================================
# DEF-007: simple_calculator must never execute arbitrary Python via eval()
# =============================================================================

def test_simple_calculator_still_does_basic_arithmetic():
    assert simple_calculator.func(expression="2 + 2") == "Result: 4"
    assert simple_calculator.func(expression="10 * 5") == "Result: 50"
    assert simple_calculator.func(expression="(3 + 4) * 2") == "Result: 14"


def test_simple_calculator_rejects_code_execution_payloads():
    """A crafted expression must never be able to reach __import__, file
    I/O, attribute traversal to __class__/__globals__, etc."""
    dangerous_payloads = [
        "__import__('os').system('echo pwned')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "[x for x in range(3)]",
        "1 if True else 2",
    ]
    for payload in dangerous_payloads:
        result = simple_calculator.func(expression=payload)
        assert "Error evaluating expression" in result, f"payload not rejected: {payload!r}"


# =============================================================================
# DEF-011: GET /edp/audit?action=<invalid> must be 422, not an unhandled 500
# =============================================================================

@pytest.fixture
def audit_api_client():
    app = FastAPI()
    app.include_router(audit_router, prefix="/edp")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_audit_endpoint_rejects_invalid_action_with_422(audit_api_client):
    async with audit_api_client as client:
        resp = await client.get("/edp/audit", params={"action": "NOT_A_REAL_ACTION"})
    assert resp.status_code == 422
    assert "NOT_A_REAL_ACTION" in resp.json()["detail"]


async def test_audit_endpoint_accepts_valid_action(audit_api_client):
    async with audit_api_client as client:
        resp = await client.get("/edp/audit", params={"action": "WORKFLOW_UPLOAD"})
    assert resp.status_code == 200


async def test_audit_endpoint_accepts_no_action_filter(audit_api_client):
    async with audit_api_client as client:
        resp = await client.get("/edp/audit")
    assert resp.status_code == 200


# =============================================================================
# DEF-013 / DEF-014: generic HealthChecker database check + readiness gating
# =============================================================================

async def test_health_checker_database_check_uses_real_edp_connectivity(monkeypatch):
    from src.utils.health import HealthChecker, HealthStatus

    async def fake_ok():
        return {"status": "ok", "latency_ms": 5}

    monkeypatch.setattr("src.agent.edp.database.check_connectivity", fake_ok)
    checker = HealthChecker()
    component = await checker.check_database_connectivity()
    assert component.status == HealthStatus.HEALTHY
    assert component.details["type"] == "postgresql"


async def test_health_checker_database_check_reports_unhealthy_on_failure(monkeypatch):
    from src.utils.health import HealthChecker, HealthStatus

    async def fake_fail():
        return {"status": "error", "error": "connection refused", "latency_ms": 5}

    monkeypatch.setattr("src.agent.edp.database.check_connectivity", fake_fail)
    checker = HealthChecker()
    component = await checker.check_database_connectivity()
    assert component.status == HealthStatus.UNHEALTHY


async def test_readiness_fails_when_database_is_down(monkeypatch):
    """DEF-014: is_ready() must gate on the EDP database, not just the LLM
    key — without a reachable DB every /edp/* call fails outright."""
    from src.utils.health import HealthChecker

    checker = HealthChecker()

    async def fake_llm_ok():
        from src.utils.health import ComponentHealth, HealthStatus
        return ComponentHealth(name="llm", status=HealthStatus.HEALTHY)

    async def fake_db_down():
        from src.utils.health import ComponentHealth, HealthStatus
        return ComponentHealth(name="database", status=HealthStatus.UNHEALTHY)

    monkeypatch.setattr(checker, "check_llm_availability", fake_llm_ok)
    monkeypatch.setattr(checker, "check_database_connectivity", fake_db_down)
    monkeypatch.setattr(checker, "check_tools_readiness", fake_llm_ok)
    monkeypatch.setattr(checker, "check_observability", fake_llm_ok)
    monkeypatch.setattr(checker, "check_metrics", fake_llm_ok)
    monkeypatch.setattr(checker, "check_rate_limiter", fake_llm_ok)

    assert await checker.is_ready() is False


def test_metrics_and_rate_limiter_disabled_by_default():
    """The features backing these checks (src/utils/metrics.py,
    src/middleware/rate_limiting.py) don't exist in this EDP-specific
    build — defaulting them off keeps /health from permanently reporting
    a false-positive degraded status."""
    from src.config.settings import Settings

    fresh = Settings(_env_file=None)
    assert fresh.metrics_enabled is False
    assert fresh.rate_limit_enabled is False


# =============================================================================
# DEF-015: POST /agent/run must return real HTTP error codes, not 200+error
# =============================================================================

def test_agent_run_missing_query_returns_400():
    """build_app() pulls in the full a2a-sdk stack (unrelated to this fix) —
    skip gracefully in dev environments where that optional/pinned
    dependency isn't fully installed, same precedent as
    tests/agent_tests/* (see README.md)."""
    a2a_apps = pytest.importorskip("a2a.server.apps")
    del a2a_apps
    from fastapi.testclient import TestClient
    from src.agent.__main__ import build_app

    app = build_app()
    with TestClient(app) as client:
        resp = client.post("/agent/run", json={})
    assert resp.status_code == 400


# =============================================================================
# DEF-016: retry/skip on a nonexistent segment must be 404, not 409
# =============================================================================

@pytest.fixture
def status_api_client():
    app = FastAPI()
    app.include_router(status_router, prefix="/edp")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _far_future_date() -> date:
    return date.today().replace(year=date.today().year + 20)


async def test_retry_nonexistent_segment_returns_404(status_api_client):
    trade_date = _far_future_date()
    async with status_api_client as client:
        resp = await client.post(f"/edp/status/{trade_date.isoformat()}/EQ/retry")
    assert resp.status_code == 404


async def test_skip_nonexistent_segment_returns_404(status_api_client):
    trade_date = _far_future_date()
    async with status_api_client as client:
        resp = await client.post(
            f"/edp/status/{trade_date.isoformat()}/EQ/skip",
            json={"reason": "test", "skipped_by": "test"},
        )
    assert resp.status_code == 404


async def test_retry_existing_but_wrong_status_segment_returns_409(session_factory, test_date, status_api_client):
    async with session_factory() as session:
        row = SegmentExecution(
            id=str(uuid.uuid4()),
            trade_date=test_date,
            segment_code="EQ",
            config_id_used="cfg-1",
            segment_status=SegmentStatus.PENDING,  # not FAILED/SKIPPED -> retry must refuse
            processes_json={},
        )
        session.add(row)
        await session.commit()

    async with status_api_client as client:
        resp = await client.post(f"/edp/status/{test_date.isoformat()}/EQ/retry")
    assert resp.status_code == 409


# =============================================================================
# DEF-018: get_edp_status must resolve segment aliases (CASH -> EQ)
# =============================================================================

async def test_get_edp_status_resolves_cash_alias_to_eq(monkeypatch, test_date):
    from src.tools import edp_status

    captured_paths = []

    async def fake_get(path):
        captured_paths.append(path)
        return 404, {}

    monkeypatch.setattr(edp_status, "_get", fake_get)
    await edp_status.get_edp_status.coroutine(trade_date=test_date.isoformat(), segment_code="CASH")

    assert captured_paths == [f"/edp/status/{test_date.isoformat()}/EQ"]
