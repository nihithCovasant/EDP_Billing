"""
Chat-tool-level behavior for src/tools/edp_health.py (check_edp_system_health).
"""

from __future__ import annotations

import src.tools.edp_health as edp_health


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


async def test_healthy_system_reports_all_green(monkeypatch):
    async def fake_get(path):
        assert path == "/edp/health"
        return 200, {
            "status": "healthy",
            "billing_loop": {"running": True, "alive": True},
            "database": {"status": "ok"},
            "cbos": {"status": "ok"},
            "alerts": {"last_attempt_at": None},
        }

    monkeypatch.setattr(edp_health, "_get", fake_get)
    monkeypatch.setattr(edp_health.os, "getenv", lambda k, d="": d)

    result = await _invoke(edp_health.check_edp_system_health)

    assert "HEALTHY" in result
    assert "✅" in result
    assert "❌" not in result


async def test_unhealthy_cbos_is_flagged(monkeypatch):
    async def fake_get(path):
        return 503, {
            "status": "unhealthy",
            "billing_loop": {"running": True, "alive": True},
            "database": {"status": "ok"},
            "cbos": {"status": "error", "status_url": {"ok": False, "error": "timeout"}},
            "alerts": {},
        }

    monkeypatch.setattr(edp_health, "_get", fake_get)
    monkeypatch.setattr(edp_health.os, "getenv", lambda k, d="": d)

    result = await _invoke(edp_health.check_edp_system_health)

    assert "UNHEALTHY" in result
    assert "CBOS" in result and "error" in result


async def test_mock_cbos_mode_is_not_treated_as_unhealthy(monkeypatch):
    async def fake_get(path):
        return 200, {
            "status": "healthy",
            "billing_loop": {"running": True, "alive": True},
            "database": {"status": "ok"},
            "cbos": {"status": "mock"},
            "alerts": {},
        }

    monkeypatch.setattr(edp_health, "_get", fake_get)
    monkeypatch.setattr(edp_health.os, "getenv", lambda k, d="": d)

    result = await _invoke(edp_health.check_edp_system_health)

    assert "mock mode" in result
    assert "✅ **CBOS:**" in result


async def test_dry_run_flag_surfaced_when_env_set(monkeypatch):
    async def fake_get(path):
        return 200, {
            "status": "healthy",
            "billing_loop": {"running": True, "alive": True},
            "database": {"status": "ok"},
            "cbos": {"status": "ok"},
            "alerts": {},
        }

    monkeypatch.setattr(edp_health, "_get", fake_get)
    monkeypatch.setenv("EMAIL_DRY_RUN", "true")

    result = await _invoke(edp_health.check_edp_system_health)

    assert "DRY-RUN mode" in result


async def test_dry_run_flag_absent_when_env_unset(monkeypatch):
    async def fake_get(path):
        return 200, {
            "status": "healthy",
            "billing_loop": {"running": True, "alive": True},
            "database": {"status": "ok"},
            "cbos": {"status": "ok"},
            "alerts": {},
        }

    monkeypatch.setattr(edp_health, "_get", fake_get)
    monkeypatch.delenv("EMAIL_DRY_RUN", raising=False)

    result = await _invoke(edp_health.check_edp_system_health)

    assert "DRY-RUN mode" not in result
