"""V5 payload-shape pins for the engine's CBOS client (wayfinder ticket 14).

The V5 doc requires TradeDate (YYYY-MM-DD) in EVERY file_process_status call:
Shape A (Segment, TradeDate, ProcessName, UserID) for the real-segment steps,
Shape B (TradeDate, ProcessName, UserID — no Segment) for the post-trade
GTG/already-triggered/completion checks. These tests capture the ACTUAL JSON
the client sends (stubbed httpx transport) so a regression to the v3 shapes —
which real v5 CBOS may resolve against the WRONG DAY's process — cannot land
silently. Payload construction itself comes from edpb_core.cbos, shared with
the uploader.
"""

from __future__ import annotations

from datetime import date

import pytest

import src.tools.cbos_client as cbos_module
from src.tools.cbos_client import CbosClient

TRADE_DATE = date(2026, 7, 20)


class _StubResponse:
    status_code = 200
    text = '{"Status":"Success","Data":[{"MSG":"TRUE"}]}'


class _CapturingAsyncClient:
    """Stands in for httpx.AsyncClient: records every POST's json payload."""

    captured: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        _CapturingAsyncClient.captured.append((url, json))
        return _StubResponse()


@pytest.fixture()
def captured(monkeypatch):
    _CapturingAsyncClient.captured = []
    monkeypatch.setattr(cbos_module.httpx, "AsyncClient", _CapturingAsyncClient)
    return _CapturingAsyncClient.captured


@pytest.fixture()
def client():
    return CbosClient("http://status.test", "http://process.test", use_mock=False)


async def test_shape_a_real_segment_status_calls(client, captured):
    await client.file_process_status(
        segment="MCX", process_name="FILEUPLOAD", user_id="CV0001", trade_date=TRADE_DATE,
    )
    url, payload = captured[0]
    assert url.endswith("/api/edp/file_process_status")
    assert payload == {
        "Segment": "MCX",
        "TradeDate": "2026-07-20",
        "ProcessName": "FILEUPLOAD",
        "UserID": "CV0001",
    }
    # The doc specifies TradeDate immediately after Segment — pin the order.
    assert list(payload) == ["Segment", "TradeDate", "ProcessName", "UserID"]


async def test_shape_b_post_trade_status_calls(client, captured):
    await client.file_process_status(
        segment="DMSTMT", process_name="DAILYMARGINSTATEMENT", user_id="G_LID",
        trade_date=TRADE_DATE, include_segment=False,
    )
    _url, payload = captured[0]
    assert payload == {
        "TradeDate": "2026-07-20",
        "ProcessName": "DAILYMARGINSTATEMENT",
        "UserID": "G_LID",
    }
    assert "Segment" not in payload
    assert list(payload) == ["TradeDate", "ProcessName", "UserID"]


async def test_already_triggered_checks_use_shape_b(client, captured):
    await client.check_mtf_fund_transfer_triggered("G_LID", TRADE_DATE)
    _url, payload = captured[0]
    assert payload == {
        "TradeDate": "2026-07-20",
        "ProcessName": "MTFFUNDTRAN",
        "UserID": "G_LID",
    }


async def test_legacy_call_without_trade_date_sends_v3_and_logs(client, captured, caplog):
    """Transitional guardrail: a caller that forgot trade_date still works
    (v3 payload) but the omission is logged as an ERROR — it must never
    happen silently on the billing path."""
    await client.file_process_status(segment="EQ", process_name="RECON", user_id="CV0001")
    _url, payload = captured[0]
    assert payload == {"Segment": "EQ", "ProcessName": "RECON", "UserID": "CV0001"}
    assert any(
        "WITHOUT trade_date" in r.message for r in caplog.records
    ), "the legacy path must log loudly"


async def test_dmstmt_trigger_uses_shape_b(client, captured):
    """Review finding: V5 STEP 38 (the DMSTMT trigger) was the one
    file_process_status caller still sending the legacy v3 payload."""
    await client.trigger_daily_margin_statements("G_LID", TRADE_DATE)
    _url, payload = captured[0]
    assert payload == {
        "TradeDate": "2026-07-20",
        "ProcessName": "DAILYMARGINSTATEMENT",
        "UserID": "G_LID",
    }


async def test_get_new_trade_process_uses_v5_builder(client, captured, monkeypatch):
    monkeypatch.setattr(client, "password", "pw-under-test")
    await client.get_new_trade_process("MCX", "CV0001", TRADE_DATE, process_id="17658")
    url, payload = captured[0]
    assert url.endswith("/v1/api/process/getNewTradeProcess")
    assert payload == {
        "GROUPNAME": "MCX",
        "LOGINID": "CV0001",
        "PASSWORD": "pw-under-test",  # v5 field the v3 inline dict omitted
        "TRADEDATE": "2026-07-20",
        "PROCESSID": "17658",
    }


async def test_get_existing_process_id_uses_v5_builder(client, captured):
    await client.get_existing_process_id("MCX", "CV0001", TRADE_DATE)
    url, payload = captured[0]
    assert url.endswith("/v1/api/brokerage/getdropdown")
    assert payload["TAG"] == "EXISTINGPROCESSID"
    assert payload["FILTER1"] == "MCX"
    assert payload["FILTER2"] == "2026-07-20"
