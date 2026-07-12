"""
Error-handling / crash-hunting tests for cbos_client's response parsers.

Deliberately forces _parse_msg, _parse_new_trade_process,
_parse_already_triggered_sentence, and _parse_post_trade_trigger through a
battery of malformed CBOS response shapes (equivalence partitioning over
"malformed response shapes") to confirm each either:
  (a) degrades gracefully — returns a typed default/error result, or
  (b) crashes — raises an unhandled exception out of the parser itself.

Where a parser raises, we additionally trace the call chain to check
whether the exception is caught one level up (inside the async client
methods that call it, e.g. get_new_trade_process()/file_process_status()),
or whether it would propagate uncaught and crash a whole wake cycle.

Does NOT duplicate tests/test_cbos_client_parsing.py, which already covers:
  - _parse_already_triggered_sentence's documented/undocumented sentences
  - _is_transient_http_status classification
  - _parse_new_trade_process: unparseable-JSON (transient) vs explicit
    CBOS Status != "Success" rejection (permanent)

This file focuses on the specific malformed *shapes* listed in the task:
empty string, empty object, empty Data array, empty dict in Data, null
MSG, non-string MSG (int/list), truncated JSON, HTML error pages, a
top-level JSON array, and a very large Data array.
"""

from __future__ import annotations

import time

import pytest

from src.tools.cbos_client import (
    CbosClient,
    _parse_already_triggered_sentence,
    _parse_msg,
    _parse_new_trade_process,
    _parse_post_trade_trigger,
)


# =============================================================================
# Shared malformed-input fixtures (equivalence classes)
# =============================================================================

EMPTY_STRING = ""
EMPTY_OBJECT = "{}"
DATA_EMPTY_ARRAY = '{"Status":"Success","Data":[]}'
DATA_EMPTY_DICT_IN_ARRAY = '{"Status":"Success","Data":[{}]}'
DATA_MSG_NULL = '{"Status":"Success","Data":[{"MSG":null}]}'
DATA_MSG_NUMBER = '{"Status":"Success","Data":[{"MSG":12345}]}'
DATA_MSG_NESTED_LIST = '{"Status":"Success","Data":[{"MSG":["nested","list"]}]}'
TRUNCATED_JSON = '{"Data": [{"MSG": "TRUE"'
HTML_ERROR_PAGE = "<html><body>502 Bad Gateway</body></html>"
TOP_LEVEL_ARRAY = "[1,2,3]"


def _huge_data_array_body(n: int = 100_000) -> str:
    """A Data array with n elements — confirms parsing completes quickly
    rather than hanging on a pathologically large payload."""
    import json as _json
    return _json.dumps({"Status": "Success", "Data": [{"MSG": "TRUE"}] * n})


# =============================================================================
# _parse_msg  (file_process_status response parser)
# =============================================================================
# _parse_msg has a broad `except Exception` fallback that string-searches the
# raw body for SKIP/TRUE/FALSE, defaulting to "FALSE" — so it is expected to
# be graceful for nearly everything, INCLUDING inputs that aren't valid JSON
# at all. The interesting question is whether well-formed-but-wrong-shaped
# JSON (valid JSON, but MSG has the wrong type) still hits that fallback or
# crashes before reaching it (e.g. `.upper()` on a non-string).

def test_parse_msg_empty_string_falls_back_to_false():
    assert _parse_msg(EMPTY_STRING) == "FALSE"


def test_parse_msg_empty_object_falls_back_to_false():
    """{} has no "Data" key -> data["Data"] raises KeyError inside the
    try block, caught by the broad `except Exception`, falling back to a
    string search of the raw body (no SKIP/TRUE/FALSE substring) -> FALSE."""
    assert _parse_msg(EMPTY_OBJECT) == "FALSE"


def test_parse_msg_data_empty_array_falls_back_to_false():
    """Data[0] on an empty list raises IndexError -> caught -> fallback -> FALSE."""
    assert _parse_msg(DATA_EMPTY_ARRAY) == "FALSE"


def test_parse_msg_data_empty_dict_in_array_falls_back_to_false():
    """Data[0]["MSG"] raises KeyError when the dict has no MSG key ->
    caught -> fallback string search -> FALSE (raw body contains none of
    SKIP/TRUE/FALSE)."""
    assert _parse_msg(DATA_EMPTY_DICT_IN_ARRAY) == "FALSE"


def test_parse_msg_msg_null_returns_false_gracefully():
    """MSG present but null: `msg.upper() if msg else "FALSE"` short-circuits
    on falsy None, returning "FALSE" without ever calling .upper(). Graceful
    by design, not by accident."""
    assert _parse_msg(DATA_MSG_NULL) == "FALSE"


def test_parse_msg_msg_number_does_not_crash():
    """
    MSG is an int (12345), which is truthy, so the code takes the
    `msg.upper()` branch. int has no .upper() method, which would raise
    AttributeError -- but _parse_msg wraps the whole parse in a broad
    `except Exception`, so this is caught and falls back to a string
    search of the raw body. The raw body text contains no SKIP/TRUE/FALSE
    substring, so it defaults to "FALSE". Confirms the AttributeError does
    NOT propagate out of _parse_msg.
    """
    assert _parse_msg(DATA_MSG_NUMBER) == "FALSE"


def test_parse_msg_msg_nested_list_does_not_crash():
    """Same reasoning as the int case: list has no .upper(), AttributeError
    is caught by the broad except, falls back to string search -> FALSE."""
    assert _parse_msg(DATA_MSG_NESTED_LIST) == "FALSE"


def test_parse_msg_truncated_json_falls_back_to_string_search():
    """
    Truncated JSON fails _json.loads with JSONDecodeError, caught by the
    broad except. The fallback does a substring search over body.upper()
    -- and the truncated body '{"Data": [{"MSG": "TRUE"' DOES contain the
    substring "TRUE", so the fallback actually recovers the right answer
    here. Documents real (if accidental) resilience to mid-string network
    truncation for this particular case.
    """
    assert _parse_msg(TRUNCATED_JSON) == "TRUE"


def test_parse_msg_html_error_page_falls_back_to_false():
    """An HTML 502 page is not JSON -> JSONDecodeError -> caught -> string
    search finds no SKIP/TRUE/FALSE substring -> FALSE. Graceful: a
    proxy/load-balancer intercepting the request with an HTML error page
    does not crash the parser."""
    assert _parse_msg(HTML_ERROR_PAGE) == "FALSE"


def test_parse_msg_top_level_array_falls_back_to_false():
    """A bare JSON array parses fine via _json.loads but has no .get()
    method (list has no .get) -> AttributeError -> caught by broad except
    -> fallback string search -> FALSE."""
    assert _parse_msg(TOP_LEVEL_ARRAY) == "FALSE"


def test_parse_msg_huge_data_array_completes_quickly():
    """A 100k-element Data array should still parse (using only Data[0])
    in well under a second, not hang."""
    body = _huge_data_array_body(100_000)
    t0 = time.monotonic()
    result = _parse_msg(body)
    elapsed = time.monotonic() - t0
    assert result == "TRUE"
    assert elapsed < 2.0


# =============================================================================
# _parse_new_trade_process  (getNewTradeProcess response parser)
# =============================================================================
# Unlike _parse_msg, this parser's `except Exception` classifies the failure
# as success=False, is_transient=True (see docstring) -- so gracefully
# handled results here should show that specific shape, not just "no crash".

def test_parse_new_trade_process_empty_string_is_graceful_transient_failure():
    result = _parse_new_trade_process(EMPTY_STRING)
    assert result.success is False
    assert result.is_transient is True


def test_parse_new_trade_process_empty_object_is_graceful():
    """{} -> data.get("Status") is None != "Success" -> explicit
    NewTradeProcessResult(success=False, error=...) branch, NOT the except
    branch -- so is_transient defaults to False (permanent), since CBOS
    Status is treated as "explicitly not Success"."""
    result = _parse_new_trade_process(EMPTY_OBJECT)
    assert result.success is False
    assert result.is_transient is False
    assert result.error == "CBOS Status=None"


def test_parse_new_trade_process_data_shape_is_irrelevant_here():
    """
    _parse_new_trade_process only ever reads "Result" (Table1/Table2), never
    "Data" -- so a body with a "Data" key (empty array/dict/etc, the shapes
    this suite targets for the other 3 parsers) is irrelevant to this
    parser: Status=="Success" passes the only gate it checks, "Result" is
    simply absent so result.get("Result", {}) defaults to {}, and
    everything downstream (Table1/Table2/pid) falls back to its own
    defaults -- same graceful shape as the
    "Status:Success but no Result key at all" case below. Included to
    document that this parser is not exposed to the Data[0]/MSG
    malformations the other 3 parsers are tested against at all.
    """
    result = _parse_new_trade_process(DATA_EMPTY_ARRAY)
    assert result.success is True
    assert result.process_id is None
    assert result.steps == []


def test_parse_new_trade_process_status_success_but_missing_result_is_graceful():
    """Status:"Success" but no "Result" key at all -> result.get("Result", {})
    defaults to {}, table1 defaults to [{}], table2 to [] -- pid ends up
    None, is_runnable/is_auto_upload False, steps=[]. No crash, no exception
    branch needed; the .get() chain with defaults handles it entirely."""
    result = _parse_new_trade_process('{"Status":"Success"}')
    assert result.success is True
    assert result.process_id is None
    assert result.steps == []


def test_parse_new_trade_process_truncated_json_is_graceful_transient_failure():
    result = _parse_new_trade_process(TRUNCATED_JSON)
    assert result.success is False
    assert result.is_transient is True


def test_parse_new_trade_process_html_error_page_is_graceful_transient_failure():
    """An HTML 502 page fails _json.loads -> caught by the broad except ->
    classified as a transient (retry-worthy) failure, matching the
    docstring's stated intent that a garbled/unparseable body is more
    likely a one-off glitch than a permanent problem."""
    result = _parse_new_trade_process(HTML_ERROR_PAGE)
    assert result.success is False
    assert result.is_transient is True


def test_parse_new_trade_process_top_level_array_is_graceful():
    """A bare JSON array parses via _json.loads but list has no .get() ->
    AttributeError -> caught by the broad except -> transient failure,
    not a crash."""
    result = _parse_new_trade_process(TOP_LEVEL_ARRAY)
    assert result.success is False
    assert result.is_transient is True


def test_parse_new_trade_process_huge_table2_completes_quickly():
    """A getNewTradeProcess response with a 100k-row Table2 should still
    parse (building 100k NewTradeProcessStep objects) in reasonable time,
    not hang."""
    import json as _json
    row = {
        "ID": 1, "STEPNO": 1, "NAME": "X", "STATUS": "PENDING",
        "STATUSDESC": None, "UPLOADID": 0, "STARTDATETIME": None, "ENDDATETIME": None,
    }
    body = _json.dumps({
        "Status": "Success",
        "Result": {"Table1": [{"PROCESSID": 1, "ISRUNNABLE": True, "ISAUTOUPLOAD": True}],
                   "Table2": [row] * 100_000},
    })
    t0 = time.monotonic()
    result = _parse_new_trade_process(body)
    elapsed = time.monotonic() - t0
    assert result.success is True
    assert len(result.steps) == 100_000
    assert elapsed < 5.0


# =============================================================================
# _parse_already_triggered_sentence
# =============================================================================
# This function takes a plain `str` (already uppercased by _parse_msg's
# caller convention), not raw JSON -- so most of the "malformed JSON shape"
# equivalence classes above don't directly apply. It's a pure string
# classifier with no dict/list access, so the crash-relevant edge cases are
# about *type*, not JSON shape: what if the caller ever hands it something
# that isn't a str at all (e.g. because an upstream parser handed back None
# or a non-string MSG without going through _parse_msg's normal
# string-coercion path)?

def test_parse_already_triggered_sentence_empty_string_is_graceful():
    """Empty string matches none of the recognized patterns -> conservatively
    classified as already_triggered=True (the documented safe default)."""
    assert _parse_already_triggered_sentence(EMPTY_STRING) is True


def test_parse_already_triggered_sentence_none_crashes():
    """
    BUG: crashes on non-string input instead of returning a graceful
    default. If msg is None (e.g. an upstream MSG:null value reached this
    function directly without going through _parse_msg's `if msg else
    "FALSE"` guard), `"NOT TRIGGERED" in msg` raises TypeError: argument
    of type 'NoneType' is not iterable. There is no try/except in this
    function to catch it.

    Call-chain check: this function is only called from
    _already_triggered_via_file_status(), as
    `_parse_already_triggered_sentence(result.response)`, where
    result.response is a FileStatusResult.response: str field. In the real
    (non-mock) path, result comes from file_process_status(), which always
    constructs FileStatusResult with response=<result of _parse_msg(...)>
    or the literal "FALSE" -- both guaranteed `str`, so msg=None cannot
    occur via that call path today. The crash is real but currently
    unreachable through the production call chain; it would only surface
    if a caller ever passed a non-str response through by hand (as this
    test does directly), or if FileStatusResult were ever constructed with
    response=None elsewhere.
    """
    with pytest.raises(TypeError):
        _parse_already_triggered_sentence(None)  # type: ignore[arg-type]


def test_parse_already_triggered_sentence_non_string_number_crashes():
    """BUG: crashes on a non-string MSG value (e.g. int) the same way as
    the None case -- `"NOT TRIGGERED" in msg` raises TypeError since `in`
    requires an iterable, and ints aren't iterable. Same unreachable-via-
    production-call-chain caveat as the None case above: file_process_status
    always hands this function a str."""
    with pytest.raises(TypeError):
        _parse_already_triggered_sentence(12345)  # type: ignore[arg-type]


# =============================================================================
# _parse_post_trade_trigger
# =============================================================================
# Broad `except Exception` fallback here treats *any* unparseable-but-200
# body as success=True with the raw body (truncated) as the message -- per
# its docstring, some post-trade endpoints have no guaranteed JSON shape at
# all. So most malformed shapes are handled by design. The interesting
# question, as with _parse_msg, is whether a wrong-typed (but validly
# parsed) MSG crashes before reaching that fallback.

def test_parse_post_trade_trigger_empty_string_is_graceful():
    success, message = _parse_post_trade_trigger(EMPTY_STRING)
    assert success is True
    assert message == "Process started successfully"


def test_parse_post_trade_trigger_empty_object_is_graceful():
    """{} -> Status is None (falsy) so the rejection branch is skipped;
    Data is None (not a list) so `isinstance(items, list) and items` is
    False; msg falls through to data.get("MSG") or data.get("Message"),
    both absent -> msg stays None -> returns default success message."""
    success, message = _parse_post_trade_trigger(EMPTY_OBJECT)
    assert success is True
    assert message == "Process started successfully"


def test_parse_post_trade_trigger_data_empty_array_is_graceful():
    """Data=[] is a list but falsy (`items and items` short-circuits) ->
    same fallthrough as the empty-object case -> default success message."""
    success, message = _parse_post_trade_trigger(DATA_EMPTY_ARRAY)
    assert success is True
    assert message == "Process started successfully"


def test_parse_post_trade_trigger_data_empty_dict_in_array_is_graceful():
    """items[0].get("MSG") on {} returns None (no KeyError, .get() is
    safe) -> msg stays None -> falls through -> default success message."""
    success, message = _parse_post_trade_trigger(DATA_EMPTY_DICT_IN_ARRAY)
    assert success is True
    assert message == "Process started successfully"


def test_parse_post_trade_trigger_msg_null_is_graceful():
    """MSG explicitly null -> items[0].get("MSG") returns None -> `if not
    msg` is True -> falls through to the top-level MSG/Message lookup
    (also absent) -> default success message."""
    success, message = _parse_post_trade_trigger(DATA_MSG_NULL)
    assert success is True
    assert message == "Process started successfully"


def test_parse_post_trade_trigger_msg_number_does_not_crash():
    """
    MSG is an int (12345), which is truthy -> `msg or "Process started
    successfully"` returns the int 12345 itself (no string coercion is
    ever applied to msg in this function -- unlike _parse_msg, there's no
    `.upper()` call here). No crash, but returns message=12345 (an int),
    not a str, even though PostTradeTriggerResult.message is typed as
    `str`. Not a crash, but a latent type-contract violation worth
    flagging: any caller of PostTradeTriggerResult.message that assumes
    str (e.g. string formatting, .upper(), logging concatenation without
    str()) could still blow up downstream.
    """
    success, message = _parse_post_trade_trigger(DATA_MSG_NUMBER)
    assert success is True
    assert message == 12345


def test_parse_post_trade_trigger_msg_nested_list_does_not_crash():
    """Same reasoning: MSG=["nested","list"] is truthy -> returned as-is,
    no crash, but message ends up being a list, not a str."""
    success, message = _parse_post_trade_trigger(DATA_MSG_NESTED_LIST)
    assert success is True
    assert message == ["nested", "list"]


def test_parse_post_trade_trigger_truncated_json_is_graceful():
    """Truncated JSON fails _json.loads -> caught by the broad except ->
    returns (True, body[:200]) per the documented "no guaranteed JSON
    shape" fallback."""
    success, message = _parse_post_trade_trigger(TRUNCATED_JSON)
    assert success is True
    assert message == TRUNCATED_JSON[:200]


def test_parse_post_trade_trigger_html_error_page_is_graceful():
    """An HTML 502 page is not JSON -> caught by the broad except -> falls
    back to (True, body[:200]). Note this means an HTML error page
    intercepted by a proxy is reported as success=True with the HTML
    itself as the "message" -- not flagged as a failure at all. Not a
    crash, but arguably a silent-misclassification risk: a real upstream
    outage could be misreported as "Process started successfully"-shaped
    success up the call chain (PostTradeTriggerResult.success=True)."""
    success, message = _parse_post_trade_trigger(HTML_ERROR_PAGE)
    assert success is True
    assert message == HTML_ERROR_PAGE[:200]


def test_parse_post_trade_trigger_top_level_array_is_graceful():
    """A bare JSON array parses via _json.loads but list has no .get() ->
    AttributeError -> caught by the broad except -> falls back to (True,
    body[:200])."""
    success, message = _parse_post_trade_trigger(TOP_LEVEL_ARRAY)
    assert success is True
    assert message == TOP_LEVEL_ARRAY[:200]


def test_parse_post_trade_trigger_huge_data_array_completes_quickly():
    body = _huge_data_array_body(100_000)
    t0 = time.monotonic()
    success, message = _parse_post_trade_trigger(body)
    elapsed = time.monotonic() - t0
    assert success is True
    assert message == "TRUE"
    assert elapsed < 2.0


# =============================================================================
# Call-chain tracing: does a parser exception (where one exists) ever reach
# the async client method that calls it, or does IT have its own try/except
# that would catch it first?
# =============================================================================

async def test_already_triggered_sentence_crash_is_not_caught_by_its_only_caller():
    """
    Traces _parse_already_triggered_sentence's TypeError crash one level up
    through its sole call site, _already_triggered_via_file_status(). That
    method has NO try/except of its own around the
    `_parse_already_triggered_sentence(result.response)` call (see
    cbos_client.py) -- it only branches on `result.is_error` beforehand,
    then calls the parser unconditionally. So IF a non-str ever reached
    that call (which, per the production call chain, requires
    file_process_status() to return a non-str FileStatusResult.response --
    not possible today since it's always constructed from _parse_msg's
    str return or the literal "FALSE"), the TypeError would propagate
    straight out of _already_triggered_via_file_status() uncaught, and
    from there out of check_collateral_allocation_triggered() /
    check_mtf_fund_transfer_triggered() / check_daily_margin_statements_triggered()
    (none of which wrap the call in try/except either) -- i.e. all the way
    up to whatever calls the state machine's WAITING_FOR_GTG check, with
    no @otel_trace-level catch to stop it. This confirms the crash found
    above is not just a theoretical parser bug: nothing downstream saves it.
    """
    cbos = CbosClient("http://status", "http://process", use_mock=False)

    class _BadFileStatusResult:
        """Simulates a hypothetical FileStatusResult whose .response is
        not a str (the real dataclass is typed `str`, but nothing enforces
        that at runtime -- this stands in for that theoretical case)."""
        is_error = False
        response = None
        raw_body = ""

    async def fake_file_process_status(*, segment, process_name, user_id):
        return _BadFileStatusResult()

    import types
    cbos.file_process_status = types.MethodType(
        lambda self, *, segment, process_name, user_id: fake_file_process_status(
            segment=segment, process_name=process_name, user_id=user_id
        ),
        cbos,
    )

    with pytest.raises(TypeError):
        await cbos._already_triggered_via_file_status("DMSTMT", "CHECKDAILYMARGINSTATEMENT", "G_LID")


async def test_parse_new_trade_process_crash_class_is_fully_absorbed_by_get_new_trade_process():
    """
    Traces the OTHER direction: _parse_new_trade_process itself has its own
    broad except (confirmed above -- it never raises for any malformed
    shape tested), and get_new_trade_process() additionally wraps its
    entire httpx + parse call in a try/except that catches any residual
    Exception and returns NewTradeProcessResult(success=False,
    error=str(exc), is_transient=True). So even in the hypothetical case
    where _parse_new_trade_process's own except didn't exist, its caller
    would still absorb the failure gracefully -- this is a genuine
    defense-in-depth case, not a hidden crash. Verified here by monkeypatching
    the module-level parser to actually raise, and confirming
    get_new_trade_process still returns a graceful, non-raising result.
    """
    import httpx
    import src.tools.cbos_client as cbos_client_module

    cbos = CbosClient("http://status", "http://process", use_mock=False)

    class _FakeResponse:
        status_code = 200
        text = '{"Status":"Success","Result":{"Table1":[{"PROCESSID":1}],"Table2":[]}}'

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            return _FakeResponse()

    original_parser = cbos_client_module._parse_new_trade_process

    def _raising_parser(body):
        raise RuntimeError("simulated parser crash")

    cbos_client_module._parse_new_trade_process = _raising_parser
    original_async_client = httpx.AsyncClient
    httpx.AsyncClient = _FakeAsyncClient
    try:
        from datetime import date
        result = await cbos.get_new_trade_process("EQ", "CV0001", date(2026, 6, 29), process_id="0")
    finally:
        cbos_client_module._parse_new_trade_process = original_parser
        httpx.AsyncClient = original_async_client

    assert result.success is False
    assert result.is_transient is True
    assert "simulated parser crash" in result.error
