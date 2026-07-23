"""
Chat-tool-level behavior for src/tools/edp_download_bulk.py
(download_edp_files_bulk, download_edp_files_date_range).
"""

from __future__ import annotations

import pytest

import src.tools.edp_download_bulk as edp_download_bulk


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    monkeypatch.setattr(edp_download_bulk, "_config_value", lambda env, key, default: default)


# =============================================================================
# download_edp_files_bulk
# =============================================================================


async def test_bulk_download_calls_one_per_identifier_same_date(monkeypatch):
    calls = []

    async def fake_download_one(code, trade_date):
        calls.append((code, trade_date))
        return True, f"✅ **{code}** ({trade_date}) — success"

    monkeypatch.setattr(edp_download_bulk, "_download_one", fake_download_one)

    result = await _invoke(
        edp_download_bulk.download_edp_files_bulk,
        identifiers=["EQ", "DR"],
        trade_date="2026-07-10",
    )

    assert calls == [("EQ", "2026-07-10"), ("DR", "2026-07-10")]
    assert "2/2" in result
    assert "EQ" in result and "DR" in result


async def test_bulk_download_continues_after_one_failure(monkeypatch):
    async def fake_download_one(code, trade_date):
        if code == "EQ":
            return False, f"❌ **{code}** ({trade_date}) — timed out"
        return True, f"✅ **{code}** ({trade_date}) — success"

    monkeypatch.setattr(edp_download_bulk, "_download_one", fake_download_one)

    result = await _invoke(
        edp_download_bulk.download_edp_files_bulk,
        identifiers=["EQ", "DR"],
        trade_date="2026-07-10",
    )

    assert "1/2" in result
    assert "timed out" in result and "success" in result


async def test_bulk_download_reports_unresolved_identifiers(monkeypatch):
    async def fake_download_one(code, trade_date):
        return True, f"✅ **{code}** — success"

    monkeypatch.setattr(edp_download_bulk, "_download_one", fake_download_one)

    result = await _invoke(
        edp_download_bulk.download_edp_files_bulk,
        identifiers=["EQ", "NOTREAL"],
        trade_date="2026-07-10",
    )

    assert "NOTREAL" in result and "skipped" in result.lower()


async def test_bulk_download_rejects_empty_list():
    result = await _invoke(edp_download_bulk.download_edp_files_bulk, identifiers=[])
    assert "Please tell me" in result


# =============================================================================
# download_edp_files_date_range
# =============================================================================


async def test_date_range_calls_once_per_day_inclusive(monkeypatch):
    calls = []

    async def fake_download_one(code, trade_date):
        calls.append(trade_date)
        return True, f"✅ {trade_date}"

    monkeypatch.setattr(edp_download_bulk, "_download_one", fake_download_one)

    result = await _invoke(
        edp_download_bulk.download_edp_files_date_range,
        identifier="MCX",
        start_date="2026-07-01",
        end_date="2026-07-03",
    )

    assert calls == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert "3/3" in result


async def test_date_range_rejects_end_before_start():
    result = await _invoke(
        edp_download_bulk.download_edp_files_date_range,
        identifier="MCX",
        start_date="2026-07-10",
        end_date="2026-07-01",
    )
    assert "before start_date" in result


async def test_date_range_caps_at_31_days():
    result = await _invoke(
        edp_download_bulk.download_edp_files_date_range,
        identifier="MCX",
        start_date="2026-01-01",
        end_date="2026-12-31",
    )
    assert "capped at 31 days" in result


async def test_date_range_rejects_unresolved_identifier():
    result = await _invoke(
        edp_download_bulk.download_edp_files_date_range,
        identifier="NOTREAL",
        start_date="2026-07-01",
        end_date="2026-07-02",
    )
    assert "don't recognize" in result
