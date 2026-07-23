"""
Chat-tool-level behavior for src/tools/edp_versions.py (get_edp_active_version,
diff_edp_workflow_versions, clone_edp_workflow_version) -- same monkeypatch
convention as test_edp_chat_tools.py: fake out the module's own _get()/_post()
so no real HTTP call is involved.
"""

from __future__ import annotations

import src.tools.edp_versions as edp_versions


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


# =============================================================================
# get_edp_active_version
# =============================================================================

async def test_get_active_version_reports_name_and_counts(monkeypatch):
    async def fake_get(path):
        assert path == "/edp/workflow/2026-07-10"
        return 200, {
            "version_name": "diwali_2026", "segment_count": 9,
            "post_trade_process_count": 5, "uploaded_by": "ops",
            "uploaded_at": "2026-07-01T10:00:00+05:30", "carried_forward": False,
        }

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.get_edp_active_version, trade_date="2026-07-10")

    assert "diwali_2026" in result
    assert "9" in result
    assert "Carried forward" not in result


async def test_get_active_version_flags_carried_forward(monkeypatch):
    async def fake_get(path):
        return 200, {
            "version_name": "default", "segment_count": 9,
            "post_trade_process_count": 5, "uploaded_by": "agent-bootstrap",
            "uploaded_at": None, "carried_forward": True,
        }

    monkeypatch.setattr(edp_versions, "_get", fake_get)
    monkeypatch.setattr(edp_versions, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(edp_versions.get_edp_active_version)

    assert "Carried forward" in result


async def test_get_active_version_404_is_friendly(monkeypatch):
    async def fake_get(path):
        return 404, {"detail": "not found"}

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.get_edp_active_version, trade_date="2026-01-01")

    assert "No active workflow config" in result


# =============================================================================
# diff_edp_workflow_versions
# =============================================================================

_JSON_A = {
    "segments": [
        {"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"},
        {"segment_code": "DR", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"},
    ],
}
_JSON_B = {
    "segments": [
        {"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:30", "window_end": "18:00"},
        {"segment_code": "CUR", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"},
    ],
}


async def test_diff_two_named_versions_reports_added_removed_modified(monkeypatch):
    async def fake_get(path):
        if path == "/edp/workflow/versions/version_a":
            return 200, {"workflow_json": _JSON_A}
        if path == "/edp/workflow/versions/version_b":
            return 200, {"workflow_json": _JSON_B}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(
        edp_versions.diff_edp_workflow_versions, version_a="version_a", version_b="version_b",
    )

    assert "EQ" in result and "window_start" in result and "17:00" in result and "17:30" in result
    assert "DR" in result and "removed" in result
    assert "CUR" in result and "added" in result


async def test_diff_against_active_when_version_b_omitted(monkeypatch):
    calls = []

    async def fake_get(path):
        calls.append(path)
        if path == "/edp/workflow/versions/version_a":
            return 200, {"workflow_json": _JSON_A}
        if path == "/edp/workflow/2026-07-10":
            return 200, {"workflow_json": _JSON_B, "version_name": "current_default"}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(edp_versions, "_get", fake_get)
    monkeypatch.setattr(edp_versions, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(edp_versions.diff_edp_workflow_versions, version_a="version_a")

    assert "/edp/workflow/2026-07-10" in calls
    assert "current_default" in result


async def test_diff_identical_configs_reports_no_differences(monkeypatch):
    async def fake_get(path):
        return 200, {"workflow_json": _JSON_A}

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(
        edp_versions.diff_edp_workflow_versions, version_a="a", version_b="b",
    )

    assert "No differences" in result


async def test_diff_missing_version_a_is_friendly(monkeypatch):
    async def fake_get(path):
        return 404, {"detail": "not found"}

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.diff_edp_workflow_versions, version_a="ghost")

    assert "No saved version named" in result


# =============================================================================
# clone_edp_workflow_version
# =============================================================================

async def test_clone_fetches_source_then_uploads_as_new_name(monkeypatch):
    captured = {}

    async def fake_get(path):
        assert path == "/edp/workflow/versions/source_v"
        return 200, {"workflow_json": _JSON_A}

    async def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return 200, {"version_name": "cloned_v", "trade_date": "2026-07-10", "deferred": False}

    monkeypatch.setattr(edp_versions, "_get", fake_get)
    monkeypatch.setattr(edp_versions, "_post", fake_post)

    result = await _invoke(
        edp_versions.clone_edp_workflow_version, source_version_name="source_v", new_version_name="cloned_v",
    )

    assert captured["path"] == "/edp/workflow/upload"
    assert captured["body"]["workflow_json"] == _JSON_A
    assert captured["body"]["version_name"] == "cloned_v"
    assert "cloned_v" in result and "source_v" in result


async def test_clone_missing_source_returns_friendly_message(monkeypatch):
    async def fake_get(path):
        return 404, {"detail": "not found"}

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(
        edp_versions.clone_edp_workflow_version, source_version_name="ghost", new_version_name="new",
    )

    assert "No saved version named" in result


async def test_clone_name_conflict_returns_409_message(monkeypatch):
    async def fake_get(path):
        return 200, {"workflow_json": _JSON_A}

    async def fake_post(path, body):
        return 409, {"detail": "already exists"}

    monkeypatch.setattr(edp_versions, "_get", fake_get)
    monkeypatch.setattr(edp_versions, "_post", fake_post)

    result = await _invoke(
        edp_versions.clone_edp_workflow_version, source_version_name="source_v", new_version_name="taken",
    )

    assert "already exists" in result
