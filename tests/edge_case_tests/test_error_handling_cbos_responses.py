"""
Error-handling / crash-hunting tests for cbos_client's response parsers.

Forces _parse_msg, _parse_new_trade_process,
_parse_already_triggered_sentence, and _parse_post_trade_trigger through a
battery of malformed CBOS response shapes (empty string/object/array, null
or wrong-typed MSG, truncated JSON, HTML error pages, a top-level array, a
very large array) to confirm each either degrades gracefully (typed
default/error result) or crashes. Where a parser could raise, also traces
whether the caller (e.g. get_new_trade_process()) absorbs it.

Complements tests/test_cbos_client_parsing.py (documented sentences,
_is_transient_http_status, transient-vs-permanent classification).
"""

from __future__ import annotations

import time
from datetime import date

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
    """{} has no "Data" key -> KeyError caught by the broad except ->
    string-search fallback finds no SKIP/TRUE/FALSE -> FALSE."""
    assert _parse_msg(EMPTY_OBJECT) == "FALSE"


def test_parse_msg_data_empty_array_falls_back_to_false():
    """Data[0] on an empty list raises IndexError -> caught -> FALSE."""
    assert _parse_msg(DATA_EMPTY_ARRAY) == "FALSE"


def test_parse_msg_data_empty_dict_in_array_falls_back_to_false():
    """No MSG key -> KeyError caught -> string-search fallback -> FALSE."""
    assert _parse_msg(DATA_EMPTY_DICT_IN_ARRAY) == "FALSE"


def test_parse_msg_msg_null_returns_false_gracefully():
    """MSG present but null: `msg.upper() if msg else "FALSE"` short-circuits
    on falsy None before ever calling .upper(). Graceful by design."""
    assert _parse_msg(DATA_MSG_NULL) == "FALSE"


def test_parse_msg_msg_number_does_not_crash():
    """MSG is a truthy int (12345) -> `.upper()` would raise AttributeError,
    but the broad except catches it and falls back to string-search -> FALSE."""
    assert _parse_msg(DATA_MSG_NUMBER) == "FALSE"


def test_parse_msg_msg_nested_list_does_not_crash():
    """Same as the int case: list has no .upper(), caught -> FALSE."""
    assert _parse_msg(DATA_MSG_NESTED_LIST) == "FALSE"


def test_parse_msg_truncated_json_falls_back_to_string_search():
    """Truncated JSON fails to parse -> caught -> string-search fallback.
    The truncated body happens to contain "TRUE" as a substring, so the
    fallback recovers the right answer here — accidental resilience."""
    assert _parse_msg(TRUNCATED_JSON) == "TRUE"


def test_parse_msg_html_error_page_falls_back_to_false():
    """An HTML error page isn't JSON -> caught -> no SKIP/TRUE/FALSE
    substring found -> FALSE. A proxy error page doesn't crash the parser."""
    assert _parse_msg(HTML_ERROR_PAGE) == "FALSE"


def test_parse_msg_top_level_array_falls_back_to_false():
    """A bare JSON array parses fine but has no .get() -> AttributeError
    caught by the broad except -> fallback -> FALSE."""
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
    """{} -> Status is None != "Success" -> explicit success=False branch
    (not the except branch), so is_transient defaults to False (permanent)."""
    result = _parse_new_trade_process(EMPTY_OBJECT)
    assert result.success is False
    assert result.is_transient is False
    assert result.error == "CBOS Status=None"


def test_parse_new_trade_process_data_shape_is_irrelevant_here():
    """This parser only reads "Result" (Table1/Table2), never "Data" — a
    body with a "Data" key is irrelevant here; Result is simply absent and
    everything downstream falls back to its own defaults."""
    result = _parse_new_trade_process(DATA_EMPTY_ARRAY)
    assert result.success is True
    assert result.process_id is None
    assert result.steps == []


def test_parse_new_trade_process_status_success_but_missing_result_is_graceful():
    """Status:"Success" but no "Result" key -> the .get() chain's defaults
    handle it entirely: pid None, is_runnable/is_auto_upload False, steps=[]."""
    result = _parse_new_trade_process('{"Status":"Success"}')
    assert result.success is True
    assert result.process_id is None
    assert result.steps == []


def test_parse_new_trade_process_truncated_json_is_graceful_transient_failure():
    result = _parse_new_trade_process(TRUNCATED_JSON)
    assert result.success is False
    assert result.is_transient is True


def test_parse_new_trade_process_html_error_page_is_graceful_transient_failure():
    """An HTML error page fails to parse -> caught by the broad except ->
    classified as transient (more likely a glitch than a real rejection)."""
    result = _parse_new_trade_process(HTML_ERROR_PAGE)
    assert result.success is False
    assert result.is_transient is True


def test_parse_new_trade_process_top_level_array_is_graceful():
    """A bare JSON array parses but list has no .get() -> AttributeError
    caught by the broad except -> transient failure, not a crash."""
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
# caller convention), not raw JSON, so the malformed-JSON-shape classes
# above don't apply. Crash-relevant edge cases are about *type*: what if
# the caller ever hands it something that isn't a str at all?

def test_parse_already_triggered_sentence_empty_string_is_graceful():
    """Empty string matches no recognized pattern -> conservatively
    classified as already_triggered=True (documented safe default)."""
    assert _parse_already_triggered_sentence(EMPTY_STRING) is True


def test_parse_already_triggered_sentence_none_is_graceful():
    """An explicit isinstance guard returns True ("already triggered", the
    safe direction for a double-fire guard) for non-str input like None,
    instead of raising TypeError from `"NOT TRIGGERED" in msg`."""
    assert _parse_already_triggered_sentence(None) is True  # type: ignore[arg-type]


def test_parse_already_triggered_sentence_non_string_number_is_graceful():
    """Same isinstance guard covers a non-string MSG (int) too."""
    assert _parse_already_triggered_sentence(12345) is True  # type: ignore[arg-type]


# =============================================================================
# _parse_post_trade_trigger
# =============================================================================
# The broad `except Exception` fallback here treats most unparseable-but-200
# bodies as success=True with the raw body as the message (some post-trade
# endpoints have no guaranteed JSON shape). The deliberate exception: a body
# that looks like an HTML error page is reported as transient failure
# instead of a false "success" (_looks_like_html_error_page).

def test_parse_post_trade_trigger_empty_string_is_graceful():
    success, message, is_transient = _parse_post_trade_trigger(EMPTY_STRING)
    assert success is True
    assert message == "Process started successfully"
    assert is_transient is False


def test_parse_post_trade_trigger_empty_object_is_graceful():
    """{} -> Status is None (falsy, rejection branch skipped); Data absent
    -> msg stays None -> default success message."""
    success, message, is_transient = _parse_post_trade_trigger(EMPTY_OBJECT)
    assert success is True
    assert message == "Process started successfully"
    assert is_transient is False


def test_parse_post_trade_trigger_data_empty_array_is_graceful():
    """Data=[] is falsy -> same fallthrough as empty-object -> default
    success message."""
    success, message, is_transient = _parse_post_trade_trigger(DATA_EMPTY_ARRAY)
    assert success is True
    assert message == "Process started successfully"
    assert is_transient is False


def test_parse_post_trade_trigger_data_empty_dict_in_array_is_graceful():
    """items[0].get("MSG") on {} returns None (safe) -> default success message."""
    success, message, is_transient = _parse_post_trade_trigger(DATA_EMPTY_DICT_IN_ARRAY)
    assert success is True
    assert message == "Process started successfully"
    assert is_transient is False


def test_parse_post_trade_trigger_msg_null_is_graceful():
    """MSG explicitly null -> falls through to the top-level MSG/Message
    lookup (also absent) -> default success message."""
    success, message, is_transient = _parse_post_trade_trigger(DATA_MSG_NULL)
    assert success is True
    assert message == "Process started successfully"
    assert is_transient is False


def test_parse_post_trade_trigger_msg_number_does_not_crash():
    """MSG is a truthy int -> returned as-is (no str coercion in this
    function, unlike _parse_msg's .upper()). No crash, but message ends up
    an int, not a str, despite PostTradeTriggerResult.message being typed
    `str` — a latent type-contract violation for any caller assuming str."""
    success, message, is_transient = _parse_post_trade_trigger(DATA_MSG_NUMBER)
    assert success is True
    assert message == 12345
    assert is_transient is False


def test_parse_post_trade_trigger_msg_nested_list_does_not_crash():
    """Same reasoning: a truthy list MSG is returned as-is, no crash, but
    message ends up being a list, not a str."""
    success, message, is_transient = _parse_post_trade_trigger(DATA_MSG_NESTED_LIST)
    assert success is True
    assert message == ["nested", "list"]
    assert is_transient is False


def test_parse_post_trade_trigger_truncated_json_is_graceful():
    """Truncated JSON fails to parse -> caught -> doesn't look like an
    HTML error page -> falls back to (True, body[:200], False)."""
    success, message, is_transient = _parse_post_trade_trigger(TRUNCATED_JSON)
    assert success is True
    assert message == TRUNCATED_JSON[:200]
    assert is_transient is False


def test_parse_post_trade_trigger_html_error_page_is_now_reported_as_failure():
    """An HTML error page is recognized by _looks_like_html_error_page()
    and reported as a transient failure rather than a false "success" —
    a real proxy/LB outage gets retried instead of misreported as done."""
    success, message, is_transient = _parse_post_trade_trigger(HTML_ERROR_PAGE)
    assert success is False
    assert is_transient is True
    assert message != HTML_ERROR_PAGE[:200]


def test_parse_post_trade_trigger_top_level_array_is_graceful():
    """A bare JSON array parses but list has no .get() -> caught -> not an
    HTML error page -> falls back to (True, body[:200], False)."""
    success, message, is_transient = _parse_post_trade_trigger(TOP_LEVEL_ARRAY)
    assert success is True
    assert message == TOP_LEVEL_ARRAY[:200]
    assert is_transient is False


def test_parse_post_trade_trigger_huge_data_array_completes_quickly():
    body = _huge_data_array_body(100_000)
    t0 = time.monotonic()
    success, message, is_transient = _parse_post_trade_trigger(body)
    elapsed = time.monotonic() - t0
    assert success is True
    assert message == "TRUE"
    assert is_transient is False
    assert elapsed < 2.0


# =============================================================================
# Call-chain tracing: does a parser exception (where one exists) ever reach
# the async client method that calls it, or does IT have its own try/except
# that would catch it first?
# =============================================================================

async def test_already_triggered_sentence_non_string_is_absorbed_by_its_only_caller():
    """Traces the isinstance guard one level up through its sole caller,
    _already_triggered_via_file_status(): a non-str FileStatusResult.response
    is absorbed by the guard, returning already_triggered=True instead of
    raising TypeError up through the WAITING_FOR_GTG check."""
    cbos = CbosClient("http://status", "http://process", use_mock=False)

    class _BadFileStatusResult:
        """Simulates a hypothetical FileStatusResult whose .response is
        not a str (the real dataclass is typed `str`, but nothing enforces
        that at runtime -- this stands in for that theoretical case)."""
        is_error = False
        response = None
        raw_body = ""

    async def fake_file_process_status(*, segment, process_name, user_id, trade_date, include_segment=True):
        return _BadFileStatusResult()

    import types
    cbos.file_process_status = types.MethodType(
        lambda self, *, segment, process_name, user_id, trade_date, include_segment=True: fake_file_process_status(
            segment=segment, process_name=process_name, user_id=user_id, trade_date=trade_date,
            include_segment=include_segment,
        ),
        cbos,
    )

    result = await cbos._already_triggered_via_file_status(
        "DMSTMT", "CHECKDAILYMARGINSTATEMENT", "G_LID", date(2026, 6, 29),
    )
    assert result.already_triggered is True


async def test_parse_new_trade_process_crash_class_is_fully_absorbed_by_get_new_trade_process():
    """The other direction: get_new_trade_process() wraps its entire httpx +
    parse call in its own try/except, so even if _parse_new_trade_process
    raised, its caller absorbs the failure — defense-in-depth. Verified by
    monkeypatching the module-level parser to actually raise."""
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
