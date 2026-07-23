"""
Chat-tool-level behavior for src/tools/edp_bulk_window.py (bulk update,
copy-window, day timeline) -- same monkeypatch convention as the other
edp_*_tools test files.
"""

from __future__ import annotations

import copy

import src.tools.edp_bulk_window as edp_bulk


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


def _today_config() -> dict:
    """Fresh deep copy every call -- tools patch this dict in-place, so a
    module-level shared instance would leak mutations across tests."""
    return copy.deepcopy({
        "segments": [
            {"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"},
            {"segment_code": "DR", "login_id": "CV0001", "window_start": "17:30", "window_end": "18:30"},
        ],
        "post_trade_processes": [
            {"process_code": "COLVAL", "login_id": "CV0001", "window_start": "02:30", "window_end": "06:00"},
        ],
    })


# =============================================================================
# update_edp_segment_windows_bulk
# =============================================================================

async def test_bulk_update_patches_multiple_targets_in_one_upload(monkeypatch):
    calls = {"get": 0, "post": 0}
    captured = {}

    async def fake_get(path):
        calls["get"] += 1
        return 200, {"workflow_json": _today_config(), "carried_forward": False}

    async def fake_post(path, body):
        calls["post"] += 1
        captured["body"] = body
        return 200, {"version_name": "bulk_v1", "trade_date": "2026-07-10", "deferred": False}

    monkeypatch.setattr(edp_bulk, "_get", fake_get)
    monkeypatch.setattr(edp_bulk, "_post", fake_post)
    monkeypatch.setattr(edp_bulk, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(
        edp_bulk.update_edp_segment_windows_bulk,
        updates=[
            {"identifier": "EQ", "window_start": "17:15"},
            {"identifier": "DR", "window_start": "17:45", "window_end": "18:45"},
        ],
        version_name="bulk_v1",
    )

    assert calls == {"get": 1, "post": 1}, "must fetch once and upload once, not per-target"
    patched_segments = {s["segment_code"]: s for s in captured["body"]["workflow_json"]["segments"]}
    assert patched_segments["EQ"]["window_start"] == "17:15"
    assert patched_segments["DR"]["window_start"] == "17:45"
    assert patched_segments["DR"]["window_end"] == "18:45"
    assert "EQ" in result and "DR" in result and "bulk_v1" in result


async def test_bulk_update_rejects_empty_list():
    result = await _invoke(edp_bulk.update_edp_segment_windows_bulk, updates=[], version_name="x")
    assert "Please tell me" in result


async def test_bulk_update_rejects_unresolvable_identifier(monkeypatch):
    result = await _invoke(
        edp_bulk.update_edp_segment_windows_bulk,
        updates=[{"identifier": "NOTASEGMENT", "window_start": "17:00"}],
        version_name="x",
    )
    assert "Couldn't process" in result


async def test_bulk_update_reports_missing_targets(monkeypatch):
    async def fake_get(path):
        return 200, {"workflow_json": _today_config(), "carried_forward": False}

    async def fake_post(path, body):
        return 200, {"version_name": "v1", "trade_date": "2026-07-10", "deferred": False}

    monkeypatch.setattr(edp_bulk, "_get", fake_get)
    monkeypatch.setattr(edp_bulk, "_post", fake_post)
    monkeypatch.setattr(edp_bulk, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(
        edp_bulk.update_edp_segment_windows_bulk,
        updates=[
            {"identifier": "EQ", "window_start": "17:15"},
            {"identifier": "MCX", "window_start": "18:00"},  # not in _today_config()
        ],
        version_name="v1",
    )

    assert "MCX" in result and "skipped" in result.lower()


# =============================================================================
# copy_edp_segment_window
# =============================================================================

async def test_copy_window_copies_start_and_end(monkeypatch):
    captured = {}

    async def fake_get(path):
        return 200, {"workflow_json": _today_config(), "carried_forward": False}

    async def fake_post(path, body):
        captured["body"] = body
        return 200, {"version_name": "copy_v1", "trade_date": "2026-07-10", "deferred": False}

    monkeypatch.setattr(edp_bulk, "_get", fake_get)
    monkeypatch.setattr(edp_bulk, "_post", fake_post)
    monkeypatch.setattr(edp_bulk, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(
        edp_bulk.copy_edp_segment_window,
        source_identifier="EQ", target_identifier="DR", version_name="copy_v1",
    )

    dr = next(s for s in captured["body"]["workflow_json"]["segments"] if s["segment_code"] == "DR")
    assert dr["window_start"] == "17:00" and dr["window_end"] == "18:00"
    assert "EQ" in result and "DR" in result


async def test_copy_window_missing_source_is_friendly(monkeypatch):
    async def fake_get(path):
        return 200, {"workflow_json": _today_config(), "carried_forward": False}

    monkeypatch.setattr(edp_bulk, "_get", fake_get)
    monkeypatch.setattr(edp_bulk, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(
        edp_bulk.copy_edp_segment_window,
        source_identifier="MCX", target_identifier="DR", version_name="v1",
    )

    assert "isn't present" in result


# =============================================================================
# get_edp_day_timeline
# =============================================================================

async def test_timeline_sorts_by_start_time(monkeypatch):
    async def fake_get(path):
        assert path == "/edp/workflow/2026-07-10"
        return 200, {"workflow_json": _today_config()}

    monkeypatch.setattr(edp_bulk, "_get", fake_get)

    result = await _invoke(edp_bulk.get_edp_day_timeline, trade_date="2026-07-10")

    colval_idx = result.index("COLVAL")
    eq_idx = result.index("EQ")
    dr_idx = result.index("DR")
    assert colval_idx < eq_idx < dr_idx  # 02:30 before 17:00 before 17:30


async def test_timeline_404_is_friendly(monkeypatch):
    async def fake_get(path):
        return 404, {"detail": "not found"}

    monkeypatch.setattr(edp_bulk, "_get", fake_get)

    result = await _invoke(edp_bulk.get_edp_day_timeline, trade_date="2026-01-01")

    assert "No active workflow config" in result
