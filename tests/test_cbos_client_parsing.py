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

    async def fake_pending(*, segment, process_name, user_id, trade_date=None, include_segment=True):
        return FileStatusResult(response="PROCESS TRIGGERED IS PENDING")

    monkeypatch.setattr(cbos, "file_process_status", fake_pending)
    result = await cbos._already_triggered_via_file_status("COLALLOC", "MTFCOLLALLOC", "G_LID")
    assert result.already_triggered is False

    async def fake_already_triggered(*, segment, process_name, user_id, trade_date=None, include_segment=True):
        return FileStatusResult(response="PROCESS ALREADY TRIGGERED")

    monkeypatch.setattr(cbos, "file_process_status", fake_already_triggered)
    result = await cbos._already_triggered_via_file_status("DMSTMT", "CHECKDAILYMARGINSTATEMENT", "G_LID")
    assert result.already_triggered is True


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
