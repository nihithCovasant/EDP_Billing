"""
Regression tests for the mock_cbos/ standalone server fixes from the second
bug-report batch: DEF-027, DEF-028, DEF-031, DEF-032, DEF-033, DEF-034.

mock_cbos has zero imports from src/, so these tests exercise it in total
isolation via httpx.ASGITransport — no real database, no agent code.
"""

from __future__ import annotations

import httpx
import pytest

from mock_cbos.main import app
from mock_cbos.state import state


@pytest.fixture(autouse=True)
def _reset_mock_state():
    state.reset()
    yield
    state.reset()


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://mock-cbos")


# =============================================================================
# DEF-027: DMRPT's real trigger endpoint (CombinedMarginProcess) must exist
# =============================================================================

async def test_combined_margin_process_trigger_endpoint_exists(client):
    async with client as c:
        resp = await c.post(
            "/v1/api/process/CombinedMarginProcess",
            json={"BUTTONNAME": "COMBINEDMARGIN_PROCESS", "LOGINID": "CV0001", "MARGINDATE": "29-Jun-2026"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["Status"] == "Success"
    assert state.is_post_trade_triggered("DMRPT")


async def test_invented_daily_margin_reporting_route_no_longer_exists(client):
    """The old invented route the agent never actually called — asserting
    its absence guards against silently reintroducing dead/misleading
    routes that don't match the real CBOS API surface."""
    async with client as c:
        resp = await c.post("/v1/api/process/DailyMarginReporting", json={})
    assert resp.status_code == 404


# =============================================================================
# DEF-031: GetCollateralValuation must dispatch on BUTTONNAME
# =============================================================================

async def test_collateral_valuation_trigger_marks_triggered(client):
    async with client as c:
        resp = await c.post(
            "/v1/api/process/GetCollateralValuation",
            json={"BUTTONNAME": "COLLATERAL_VALUATION_DATEWISE", "LOGINID": "CV0001", "MARGINDATE": "29-Jun-2026"},
        )
    assert resp.status_code == 200
    assert state.is_post_trade_triggered("COLVAL")


async def test_collateral_valuation_refresh_check_reflects_trigger_state(client):
    async with client as c:
        # Before any trigger — REFRESH must report "not triggered" (empty Table1).
        before = await c.post(
            "/v1/api/process/GetCollateralValuation",
            json={"BUTTONNAME": "REFRESH", "LOGINID": "CV0001"},
        )
        assert before.json()["Result"]["Table1"] == []

        await c.post(
            "/v1/api/process/GetCollateralValuation",
            json={"BUTTONNAME": "COLLATERAL_VALUATION_DATEWISE", "LOGINID": "CV0001", "MARGINDATE": "29-Jun-2026"},
        )

        after = await c.post(
            "/v1/api/process/GetCollateralValuation",
            json={"BUTTONNAME": "REFRESH", "LOGINID": "CV0001"},
        )
        assert len(after.json()["Result"]["Table1"]) > 0


async def test_combined_margin_process_refresh_check_reflects_trigger_state(client):
    async with client as c:
        before = await c.post(
            "/v1/api/process/CombinedMarginProcess",
            json={"BUTTONNAME": "REFRESH", "LOGINID": "CV0001"},
        )
        assert before.json()["Result"]["Table1"] == []

        await c.post(
            "/v1/api/process/CombinedMarginProcess",
            json={"BUTTONNAME": "COMBINEDMARGIN_PROCESS", "LOGINID": "CV0001", "MARGINDATE": "29-Jun-2026"},
        )

        after = await c.post(
            "/v1/api/process/CombinedMarginProcess",
            json={"BUTTONNAME": "REFRESH", "LOGINID": "CV0001"},
        )
        assert len(after.json()["Result"]["Table1"]) > 0


# =============================================================================
# DEF-028: DMSTMT's one-shot STATUS-API trigger must ack immediately, not
# participate in the generic 2-poll GTG counter.
# =============================================================================

async def test_dmstmt_trigger_process_name_succeeds_on_first_call(client):
    async with client as c:
        resp = await c.post(
            "/api/edp/file_process_status",
            json={"ProcessName": "DAILYMARGINSTATEMENT", "UserID": "CV0001", "TradeDate": "2026-06-29"},
        )
    assert resp.status_code == 200
    assert resp.json()["Data"][0]["MSG"] == "TRUE"
    assert state.is_post_trade_triggered("DMSTMT")


# =============================================================================
# DEF-032: the 3 "already triggered" file_process_status checks (COLALLOC/
# MTFFT/DMSTMT) must reflect real trigger state, not an independent poll
# counter that can drift from reality.
# =============================================================================

@pytest.mark.parametrize(
    "process_code,check_process_name,not_triggered_sentence",
    [
        ("COLALLOC", "MTFCOLLALLOC", "PROCESS TRIGGERED IS PENDING"),
        ("MTFFT", "MTFFUNDTRAN", "PROCESS TRIGGERED IS PENDING"),
        ("DMSTMT", "CHECKDAILYMARGINSTATEMENT", "DAILYMARGINSTATEMENT IS NOT TRIGGERED"),
    ],
)
async def test_already_triggered_check_tracks_real_trigger_state(
    client, process_code, check_process_name, not_triggered_sentence,
):
    async with client as c:
        # Never triggered yet — even after enough polls to have satisfied
        # the old generic 2-poll counter, must still say "not triggered".
        for _ in range(5):
            resp = await c.post(
                "/api/edp/file_process_status",
                json={"ProcessName": check_process_name, "UserID": "CV0001", "TradeDate": "2026-06-29"},
            )
            assert resp.json()["Data"][0]["MSG"] == not_triggered_sentence

        state.mark_post_trade_triggered(process_code, "CV0001")

        resp = await c.post(
            "/api/edp/file_process_status",
            json={"ProcessName": check_process_name, "UserID": "CV0001", "TradeDate": "2026-06-29"},
        )
        assert resp.json()["Data"][0]["MSG"] == "TRUE"


# =============================================================================
# DEF-033: GTG poll counters must be keyed by trade_date, not just
# (segment, process_name) — otherwise a second day in the same server
# process starts "already ready" from the first day's counter.
# =============================================================================

async def test_gtg_poll_counter_is_keyed_by_trade_date(client):
    async with client as c:
        # Day 1: exhaust the counter to TRUE.
        for _ in range(3):
            resp = await c.post(
                "/api/edp/file_process_status",
                json={"Segment": "EQ", "ProcessName": "BILLPOSTING", "UserID": "CV0001", "TradeDate": "2026-06-29"},
            )
        assert resp.json()["Data"][0]["MSG"] == "TRUE"

        # Day 2 (different TradeDate, same segment/process_name): must
        # start from zero, not instantly read as ready.
        resp = await c.post(
            "/api/edp/file_process_status",
            json={"Segment": "EQ", "ProcessName": "BILLPOSTING", "UserID": "CV0001", "TradeDate": "2026-06-30"},
        )
        assert resp.json()["Data"][0]["MSG"] == "FALSE"


# =============================================================================
# DEF-034: trigger-mode getNewTradeProcess must not return every Table2
# step as instantly SUCCESS — real CBOS runs the later calculation/posting
# steps asynchronously.
# =============================================================================

async def test_trigger_mode_table2_reflects_realistic_async_progress(client):
    async with client as c:
        reserve = await c.post(
            "/v1/api/process/getNewTradeProcess",
            json={"GROUPNAME": "EQ", "LOGINID": "CV0001", "TRADEDATE": "2026-06-29", "PROCESSID": "0"},
        )
        pid = reserve.json()["Result"]["Table1"][0]["PROCESSID"]

        trigger = await c.post(
            "/v1/api/process/getNewTradeProcess",
            json={"GROUPNAME": "EQ", "LOGINID": "CV0001", "TRADEDATE": "2026-06-29", "PROCESSID": str(pid)},
        )
    table2 = trigger.json()["Result"]["Table2"]
    statuses = {row["STATUS"] for row in table2}
    # Must be a genuine mix, not every step instantly SUCCESS.
    assert statuses == {"SUCCESS", "PENDING"}
    # The async calculation/bill-posting tail must still be PENDING.
    tail_statuses = {row["STATUS"] for row in table2 if row["STEPNO"] > 12}
    assert tail_statuses == {"PENDING"}
