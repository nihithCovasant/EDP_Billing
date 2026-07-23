"""
Chat-tool-level behavior for check_edp_version_segment_validity (src/tools/edp_versions.py).
"""

from __future__ import annotations

import src.tools.edp_versions as edp_versions


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


async def test_all_valid_codes_reports_ok(monkeypatch):
    async def fake_get(path):
        return 200, {"workflow_json": {
            "segments": [{"segment_code": "EQ"}, {"segment_code": "DR"}],
            "post_trade_processes": [{"process_code": "COLVAL"}],
        }}

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.check_edp_version_segment_validity, version_name="v1")

    assert "✅" in result and "still valid" in result


async def test_stale_segment_code_is_flagged(monkeypatch):
    async def fake_get(path):
        return 200, {"workflow_json": {
            "segments": [{"segment_code": "EQ"}, {"segment_code": "MF"}],
            "post_trade_processes": [],
        }}

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.check_edp_version_segment_validity, version_name="stale_v")

    assert "MF" in result and "⚠️" in result
    assert "EQ" not in result.split("Segments:")[1].split("\n")[0]


async def test_stale_post_trade_code_is_flagged(monkeypatch):
    async def fake_get(path):
        return 200, {"workflow_json": {
            "segments": [],
            "post_trade_processes": [{"process_code": "BOGUS_PROC"}],
        }}

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.check_edp_version_segment_validity, version_name="v1")

    assert "BOGUS_PROC" in result and "Post-trade processes" in result


async def test_missing_version_is_friendly(monkeypatch):
    async def fake_get(path):
        return 404, {"detail": "not found"}

    monkeypatch.setattr(edp_versions, "_get", fake_get)

    result = await _invoke(edp_versions.check_edp_version_segment_validity, version_name="ghost")

    assert "No saved version named" in result
