"""
Regression test for trigger_daily_margin_statements() in
src/tools/cbos_client.py.

Per EDP_Trade_Process_API_v3 STEP 38 ("EDP Status API — DAILYMARGINSTATEMENT"),
this trigger must go through the STATUS API
(POST {status_url}/api/edp/file_process_status with
{"ProcessName":"DAILYMARGINSTATEMENT","UserID":...}), the same pattern
file_process_status() uses elsewhere — NOT the {LOGINID,TRADEDATE} PROCESS
API pattern the other 4 post-trade triggers use.
"""

from __future__ import annotations

from datetime import date

from src.tools.cbos_client import CbosClient, PostTradeTriggerResult


async def test_trigger_daily_margin_statements_hits_status_api_per_step_38(monkeypatch):
    """Per STEP 38, this trigger must POST to file_process_status with
    {"ProcessName":"DAILYMARGINSTATEMENT","UserID":...}, not the process-API
    pattern the other 4 post-trade triggers use."""
    cbos = CbosClient("http://status", "http://process", use_mock=False)

    captured = {}

    async def fake_file_process_status(*, segment, process_name, user_id, trade_date, include_segment=True):
        captured["called"] = "file_process_status"
        captured["segment"] = segment
        captured["process_name"] = process_name
        captured["user_id"] = user_id
        captured["trade_date"] = trade_date
        captured["include_segment"] = include_segment
        from src.tools.cbos_client import FileStatusResult
        return FileStatusResult(response="TRUE")

    async def fake_trigger_post_trade_job(endpoint_name, login_id, trade_date, segment):
        captured["called"] = "_trigger_post_trade_job"
        captured["endpoint_name"] = endpoint_name
        captured["login_id"] = login_id
        captured["trade_date"] = trade_date
        captured["segment"] = segment
        return PostTradeTriggerResult(success=True, message="Process started successfully")

    monkeypatch.setattr(cbos, "file_process_status", fake_file_process_status)
    monkeypatch.setattr(cbos, "_trigger_post_trade_job", fake_trigger_post_trade_job)

    await cbos.trigger_daily_margin_statements("CV0001", date(2026, 6, 29))

    # Per doc STEP 38: must have gone through the STATUS API
    # (file_process_status), with ProcessName=DAILYMARGINSTATEMENT and
    # UserID=login_id -- NOT through the process-API trigger helper.
    assert captured.get("called") == "file_process_status", (
        f"Expected trigger_daily_margin_statements() to call file_process_status() "
        f"(STATUS API) per doc STEP 38, but it called {captured.get('called')!r} instead "
        f"(payload={captured})"
    )
    assert captured.get("process_name") == "DAILYMARGINSTATEMENT"
    assert captured.get("user_id") == "CV0001"
