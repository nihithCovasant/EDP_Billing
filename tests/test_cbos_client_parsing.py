"""
Unit tests for cbos_client's response parsers and request payloads for the
5 post-trade "already triggered" pre-checks:

- COLALLOC/MTFFT/DMSTMT reuse file_process_status, but its MSG is a full
  sentence, not the usual TRUE/FALSE/SKIP — see
  _parse_already_triggered_sentence(). Regression coverage for a bug
  where FileStatusResult.is_ready's strict "== TRUE" check made
  already_triggered always False, defeating the double-trigger guard for
  these 3 processes entirely.
- COLVAL/DMRPT use a REFRESH-variant call to their own trigger endpoint,
  and the exact request payload matters — CBOS is documented to have no
  input validation, so a wrong BUTTONNAME/extra field is likely to be
  silently accepted and misbehave rather than raise a clean error.
"""

from __future__ import annotations

from datetime import date

from src.tools.cbos_client import (
    CbosClient,
    FileStatusResult,
    _is_transient_http_status,
    _parse_already_triggered_sentence,
    _parse_new_trade_process,
    _parse_post_trade_trigger,
)


def test_documented_mtfcollalloc_pending_sentence_is_not_yet_triggered():
    """EDP_Trade_Process_API_v3 step 20 sample response."""
    assert _parse_already_triggered_sentence("PROCESS TRIGGERED IS PENDING ".upper()) is False


def test_documented_mtffundtran_pending_sentence_is_not_yet_triggered():
    """EDP_Trade_Process_API_v3 step 23 sample response — identical MSG to MTFCOLLALLOC."""
    assert _parse_already_triggered_sentence("PROCESS TRIGGERED IS PENDING ".upper()) is False


def test_documented_checkdailymarginstatement_not_triggered_sentence():
    """EDP_Trade_Process_API_v3 step 37 sample response."""
    assert _parse_already_triggered_sentence("DAILYMARGINSTATEMENT IS NOT TRIGGERED") is False


def test_literal_true_false_still_recognized():
    """Backward-compat with the plain TRUE/FALSE vocabulary (mocks, or if
    CBOS's real behavior ever changes to match the rest of the API)."""
    assert _parse_already_triggered_sentence("TRUE") is True
    assert _parse_already_triggered_sentence("FALSE") is False


def test_unrecognized_sentence_defaults_to_already_triggered():
    """
    The doc has no sample of the genuinely-already-triggered phrasing.
    Any sentence that isn't a recognized "not yet triggered" pattern must
    default to already_triggered=True — this check exists purely to
    prevent a double-fire, so treating an unfamiliar response as "don't
    re-fire" is the safe default, not "safe to fire again".
    """
    assert _parse_already_triggered_sentence("PROCESS ALREADY TRIGGERED") is True
    assert _parse_already_triggered_sentence("DAILYMARGINSTATEMENT IS TRIGGERED") is True


async def test_already_triggered_via_file_status_uses_sentence_parser_not_is_ready(monkeypatch):
    """
    End-to-end through _already_triggered_via_file_status(): proves the
    real (non-mock) code path classifies the documented "pending" sentence
    as NOT already triggered, and an unrecognized sentence as already
    triggered — i.e. it does NOT fall back to FileStatusResult.is_ready's
    "== TRUE" check, which would always read False for both.
    """
    cbos = CbosClient("http://status", "http://process", use_mock=False)

    async def fake_pending(*, segment, process_name, user_id, trade_date, include_segment=True):
        return FileStatusResult(response="PROCESS TRIGGERED IS PENDING")

    monkeypatch.setattr(cbos, "file_process_status", fake_pending)
    result = await cbos._already_triggered_via_file_status(
        "COLALLOC", "MTFCOLLALLOC", "G_LID", date(2026, 6, 29),
    )
    assert result.already_triggered is False

    async def fake_already_triggered(*, segment, process_name, user_id, trade_date, include_segment=True):
        return FileStatusResult(response="PROCESS ALREADY TRIGGERED")

    monkeypatch.setattr(cbos, "file_process_status", fake_already_triggered)
    result = await cbos._already_triggered_via_file_status(
        "DMSTMT", "CHECKDAILYMARGINSTATEMENT", "G_LID", date(2026, 6, 29),
    )
    assert result.already_triggered is True


async def test_already_triggered_via_file_status_omits_segment(monkeypatch):
    """MTFCOLLALLOC/MTFFUNDTRAN/CHECKDAILYMARGINSTATEMENT (doc steps 20/23/37)
    are documented WITHOUT a Segment field — confirm the shared helper
    passes include_segment=False through to file_process_status()."""
    cbos = CbosClient("http://status", "http://process", use_mock=False)
    captured = {}

    async def fake_file_process_status(*, segment, process_name, user_id, trade_date, include_segment=True):
        captured["include_segment"] = include_segment
        return FileStatusResult(response="PROCESS TRIGGERED IS PENDING")

    monkeypatch.setattr(cbos, "file_process_status", fake_file_process_status)
    await cbos._already_triggered_via_file_status("MTFFT", "MTFFUNDTRAN", "G_LID", date(2026, 6, 29))
    assert captured["include_segment"] is False


async def test_check_collateral_valuation_triggered_sends_documented_payload(monkeypatch):
    """
    EDP_Trade_Process_API_v3 step 17's documented request is exactly
    {"BUTTONNAME":"REFRESH","LOGINID":"CV0001"} — no MARGINDATE. Regression
    coverage for a bug where the code sent an invented BUTTONNAME
    ("COLLATERAL_VALUATION_REFRESH") plus an undocumented MARGINDATE field,
    which CBOS's lack of input validation would likely accept without
    complaint while silently misbehaving (e.g. always returning an empty
    Table1, making already_triggered permanently False).
    """
    cbos = CbosClient("http://status", "http://process", use_mock=False)
    captured = {}

    async def fake_check(endpoint_name, payload, segment):
        captured["endpoint_name"] = endpoint_name
        captured["payload"] = payload
        from src.tools.cbos_client import AlreadyTriggeredResult
        return AlreadyTriggeredResult(already_triggered=False)

    monkeypatch.setattr(cbos, "_already_triggered_check", fake_check)
    await cbos.check_collateral_valuation_triggered("CV0001", date(2026, 6, 29))

    assert captured["endpoint_name"] == "GetCollateralValuation"
    assert captured["payload"] == {"BUTTONNAME": "REFRESH", "LOGINID": "CV0001"}


async def test_check_daily_margin_reporting_triggered_sends_documented_payload(monkeypatch):
    """EDP_Trade_Process_API_v3 step 35 — same fix, same rationale as
    check_collateral_valuation_triggered() above."""
    cbos = CbosClient("http://status", "http://process", use_mock=False)
    captured = {}

    async def fake_check(endpoint_name, payload, segment):
        captured["endpoint_name"] = endpoint_name
        captured["payload"] = payload
        from src.tools.cbos_client import AlreadyTriggeredResult
        return AlreadyTriggeredResult(already_triggered=False)

    monkeypatch.setattr(cbos, "_already_triggered_check", fake_check)
    await cbos.check_daily_margin_reporting_triggered("CV0001", date(2026, 6, 29))

    assert captured["endpoint_name"] == "CombinedMarginProcess"
    assert captured["payload"] == {"BUTTONNAME": "REFRESH", "LOGINID": "CV0001"}


async def _captured_file_process_status_payload(cbos: CbosClient, **kwargs) -> dict:
    """Drives file_process_status() through the real (non-mock) HTTP path
    with httpx.AsyncClient faked out, and returns exactly the JSON body
    that would have been POSTed — used to assert the documented Shape A
    (Segment present) vs Shape B (Segment omitted) request shapes."""
    import httpx

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = '{"Status":"Success","Data":[{"MSG":"TRUE"}]}'

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            captured["payload"] = json
            return _FakeResponse()

    original_async_client = httpx.AsyncClient
    httpx.AsyncClient = _FakeAsyncClient
    try:
        await cbos.file_process_status(**kwargs)
    finally:
        httpx.AsyncClient = original_async_client
    return captured["payload"]


async def test_file_process_status_includes_segment_by_default_shape_a():
    """Real-segment calls (BeginFileUpload/FILEUPLOAD/BILLPOSTING/RECON/
    CONTRACTNOTEGENERATION) are documented Shape A — Segment present."""
    cbos = CbosClient("http://status", "http://process", use_mock=False)
    payload = await _captured_file_process_status_payload(
        cbos, segment="EQ", process_name="BeginFileUpload", user_id="CV0001", trade_date=date(2026, 6, 29),
    )
    assert payload == {
        "Segment": "EQ", "TradeDate": "2026-06-29", "ProcessName": "BeginFileUpload", "UserID": "CV0001",
    }


async def test_file_process_status_omits_segment_when_include_segment_false_shape_b():
    """The post-trade "Shape B" ProcessNames (CollateralValuation,
    MTFCOLLALLOC, MTFFUNDTRAN, CHECKDAILYMARGINSTATEMENT,
    DAILYMARGINSTATEMENT) are documented WITHOUT a Segment field."""
    cbos = CbosClient("http://status", "http://process", use_mock=False)
    payload = await _captured_file_process_status_payload(
        cbos, segment="DMSTMT", process_name="DAILYMARGINSTATEMENT", user_id="CV0001",
        trade_date=date(2026, 6, 29), include_segment=False,
    )
    assert payload == {"TradeDate": "2026-06-29", "ProcessName": "DAILYMARGINSTATEMENT", "UserID": "CV0001"}
    assert "Segment" not in payload


async def test_trigger_daily_margin_statements_omits_segment(monkeypatch):
    """trigger_daily_margin_statements() reuses file_process_status() —
    confirm it passes include_segment=False (doc Step 38 has no Segment)."""
    cbos = CbosClient("http://status", "http://process", use_mock=False)
    captured = {}

    async def fake_file_process_status(*, segment, process_name, user_id, trade_date, include_segment=True):
        captured["include_segment"] = include_segment
        from src.tools.cbos_client import FileStatusResult
        return FileStatusResult(response="TRUE")

    monkeypatch.setattr(cbos, "file_process_status", fake_file_process_status)
    await cbos.trigger_daily_margin_statements("CV0001", date(2026, 6, 29))
    assert captured["include_segment"] is False


async def test_documented_step24_mtf_fund_transfer_failure_is_not_read_as_success():
    """
    EDP_Trade_Process_API_v3 Step 24's documented failure keeps top-level
    Status="Success" but nests the real error in Result[].Result — e.g.
    {"Status":"Success","Result":[{"Result":"The specified @job_name
    ('MTF_RISK_UPDATE') does not exist."}]}. Regression coverage for a bug
    where _parse_post_trade_trigger only looked at Data[]/MSG/Message,
    found nothing, and fell through to "assume success."
    """
    body = (
        '{"Status":"Success","Result":[{"Result":'
        '"The specified @job_name (\'MTF_RISK_UPDATE\') does not exist."}]}'
    )
    success, message, is_transient = _parse_post_trade_trigger(body)
    assert success is False
    assert "does not exist" in message
    assert is_transient is False


def test_normal_data_msg_success_shape_still_recognized():
    """Contrast case: the documented HAPPY-path shape must still work."""
    success, message, _ = _parse_post_trade_trigger(
        '{"Status":"Success","Data":[{"MSG":"Process started successfully"}]}'
    )
    assert success is True
    assert message == "Process started successfully"


def test_http_429_is_transient_not_permanent():
    """
    A rate-limit response means "back off and retry," not "this segment
    is broken." Before the fix, only >=500 was treated as transient, so a
    429 from CBOS would fail the segment outright instead of polling
    again next cycle.
    """
    assert _is_transient_http_status(429) is True


def test_http_5xx_still_transient():
    assert _is_transient_http_status(500) is True
    assert _is_transient_http_status(503) is True


def test_http_4xx_other_than_429_still_permanent():
    """400/401/403/404 etc. mean the request itself is wrong — retrying
    without changing anything won't help, so these must stay permanent."""
    assert _is_transient_http_status(400) is False
    assert _is_transient_http_status(404) is False


def test_malformed_but_200_new_trade_process_response_is_transient():
    """
    Regression coverage: a garbled/unparseable getNewTradeProcess body
    (HTTP 200, but not valid JSON or missing expected keys) must be
    classified as transient — a one-off glitch that could well succeed on
    the next poll — not a permanent failure that fails the segment outright.
    """
    result = _parse_new_trade_process("not valid json {{{")
    assert result.success is False
    assert result.is_transient is True


def test_explicit_cbos_rejection_is_still_permanent():
    """
    Contrast case: when CBOS explicitly parses fine but reports a non-Success
    Status, that's a real rejection, not a parse glitch — must stay
    permanent (is_transient=False) so the segment fails instead of
    retrying forever against a request CBOS has already refused.
    """
    result = _parse_new_trade_process('{"Status": "Failure", "Message": "bad segment"}')
    assert result.success is False
    assert result.is_transient is False
