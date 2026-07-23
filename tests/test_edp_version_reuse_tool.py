"""
Chat-tool-level behavior for check_edp_version_name_reuse (src/tools/edp_versions.py).
"""

from __future__ import annotations

import src.tools.edp_versions as edp_versions


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


async def test_unused_name_reports_no_history(monkeypatch):
    async def fake_get(path):
        return 200, [{"version_name": "other_name", "actor": "a", "occurred_at": "t", "summary": "s"}]

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.check_edp_version_name_reuse, version_name="unused_name")

    assert "looks unused" in result


async def test_single_use_reports_no_reuse(monkeypatch):
    async def fake_get(path):
        return 200, [{"version_name": "diwali_2026", "actor": "ops", "occurred_at": "2026-07-01T10:00:00", "trade_date": "2026-07-01", "summary": "initial"}]

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.check_edp_version_name_reuse, version_name="diwali_2026")

    assert "only ONE recorded use" in result
    assert "No reuse detected" in result


async def test_multiple_uses_by_different_actors_flags_reuse(monkeypatch):
    async def fake_get(path):
        return 200, [
            {"version_name": "shared_name", "actor": "alice", "occurred_at": "2026-01-01T10:00:00", "trade_date": "2026-01-01", "summary": "s1"},
            {"version_name": "shared_name", "actor": "bob", "occurred_at": "2026-06-01T10:00:00", "trade_date": "2026-06-01", "summary": "s2"},
        ]

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.check_edp_version_name_reuse, version_name="shared_name")

    assert "2 different actors" in result
    assert "alice" in result and "bob" in result


async def test_multiple_uses_by_same_actor_does_not_flag(monkeypatch):
    async def fake_get(path):
        return 200, [
            {"version_name": "reapplied", "actor": "ops", "occurred_at": "2026-01-01T10:00:00", "trade_date": "2026-01-01", "summary": "s1"},
            {"version_name": "reapplied", "actor": "ops", "occurred_at": "2026-02-01T10:00:00", "trade_date": "2026-02-01", "summary": "s2"},
        ]

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.check_edp_version_name_reuse, version_name="reapplied")

    assert "different actors" not in result
