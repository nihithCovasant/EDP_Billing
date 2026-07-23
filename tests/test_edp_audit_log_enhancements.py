"""
Coverage for list_edp_audit_log's enhancements: action filter forwarding,
show_details field-level diff rendering, and the off-hours flag.
"""

from __future__ import annotations

import src.tools.edp_status as edp_status


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


async def test_action_filter_is_forwarded_in_query_string(monkeypatch):
    captured = {}

    async def fake_get(path):
        captured["path"] = path
        return 200, []

    monkeypatch.setattr(edp_status, "_get", fake_get)

    await _invoke(edp_status.list_edp_audit_log, action="WORKFLOW_UPLOAD")

    assert "action=WORKFLOW_UPLOAD" in captured["path"]


async def test_invalid_action_returns_422_message(monkeypatch):
    async def fake_get(path):
        return 422, {"detail": "invalid action"}

    monkeypatch.setattr(edp_status, "_get", fake_get)

    result = await _invoke(edp_status.list_edp_audit_log, action="bogus")

    assert "Unrecognized action filter" in result


async def test_show_details_renders_field_level_diff(monkeypatch):
    async def fake_get(path):
        return 200, [{
            "occurred_at": "2026-07-10T12:00:00+05:30", "actor": "ops",
            "action": "WORKFLOW_UPLOAD", "trade_date": "2026-07-10",
            "version_name": "v1", "summary": "1 change",
            "changes_json": {"segments": [
                {"code": "EQ", "change": "modified", "field": "window_start", "old": "17:00", "new": "17:30"},
            ]},
        }]

    monkeypatch.setattr(edp_status, "_get", fake_get)

    result = await _invoke(edp_status.list_edp_audit_log, show_details=True)

    assert "window_start" in result and "17:00" in result and "17:30" in result


async def test_show_details_false_omits_diff_detail(monkeypatch):
    async def fake_get(path):
        return 200, [{
            "occurred_at": "2026-07-10T12:00:00+05:30", "actor": "ops",
            "action": "WORKFLOW_UPLOAD", "trade_date": "2026-07-10",
            "version_name": "v1", "summary": "1 change",
            "changes_json": {"segments": [
                {"code": "EQ", "change": "modified", "field": "window_start", "old": "17:00", "new": "17:30"},
            ]},
        }]

    monkeypatch.setattr(edp_status, "_get", fake_get)

    result = await _invoke(edp_status.list_edp_audit_log)

    assert "17:00" not in result


async def test_off_hours_change_is_flagged(monkeypatch):
    async def fake_get(path):
        # 2026-07-11 02:00 IST is a Saturday, before business hours (09:00).
        return 200, [{
            "occurred_at": "2026-07-11T02:00:00+05:30", "actor": "ops",
            "action": "WORKFLOW_UPLOAD", "trade_date": "2026-07-11",
            "version_name": "v1", "summary": "1 change", "changes_json": {},
        }]

    monkeypatch.setattr(edp_status, "_get", fake_get)

    result = await _invoke(edp_status.list_edp_audit_log)

    assert "⚠️" in result


async def test_business_hours_change_is_not_flagged(monkeypatch):
    async def fake_get(path):
        # 2026-07-13 12:00 IST is a Monday, within business hours.
        return 200, [{
            "occurred_at": "2026-07-13T12:00:00+05:30", "actor": "ops",
            "action": "WORKFLOW_UPLOAD", "trade_date": "2026-07-13",
            "version_name": "v1", "summary": "1 change", "changes_json": {},
        }]

    monkeypatch.setattr(edp_status, "_get", fake_get)

    result = await _invoke(edp_status.list_edp_audit_log)

    assert "⚠️" not in result


async def test_sunday_change_is_flagged_regardless_of_hour(monkeypatch):
    async def fake_get(path):
        # 2026-07-12 is a Sunday.
        return 200, [{
            "occurred_at": "2026-07-12T12:00:00+05:30", "actor": "ops",
            "action": "WORKFLOW_UPLOAD", "trade_date": "2026-07-12",
            "version_name": "v1", "summary": "1 change", "changes_json": {},
        }]

    monkeypatch.setattr(edp_status, "_get", fake_get)

    result = await _invoke(edp_status.list_edp_audit_log)

    assert "⚠️" in result
