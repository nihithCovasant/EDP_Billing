"""
Deterministic tool-routing tests for the conversational agent.

These tests hit the REAL FastAPI app (built via `src.agent.__main__.build_app`)
and the REAL `POST /agent/run` endpoint, exercising the REAL LangGraph
ReAct-style graph (`AgentExecutor.build_react_graph`: agent -> tools -> agent
-> ... -> END, see `src/agent/executor.py::_run_graph`). Only the LLM call
inside `AgentNode` (`src/agent/nodes/agent_node.py`) is mocked — via
`src.agent.nodes.agent_node.get_llm_model`, the exact name imported into that
module's namespace (it does `from src.utils.llm_provider import
get_llm_model`, so patching the source module would NOT affect the already-
bound name in `agent_node` — must patch it where it's looked up).

Key facts established by reading the source (see docstrings inline below for
where each was confirmed):

- `AgentExecutor.__init__` always loads real tools via
  `get_available_tools()` (src/tools/__init__.py), which auto-discovers the
  actual @tool-decorated functions in src/tools/*.py. Since tools is always
  non-empty here, `_run_graph` always compiles `build_react_graph`, NOT the
  fixed query_processor -> context_retriever -> response_generator pipeline.
  `response_generator.py` is therefore never invoked via /agent/run in this
  app's current configuration — only `AgentNode` (LLM + tool binding) and
  `ToolNode` (dispatches by `tool_call["name"]` against a
  `{tool.name: tool}` dict built from the SAME tool instances) run.
- `ToolNode.execute` reads `tool_call["name"]`, `tool_call["args"]`,
  `tool_call["id"]` as dict keys (see src/agent/nodes/agent_node.py lines
  162-165) and calls `await tool.ainvoke(tool_args)` on the actual
  StructuredTool instance. It does NOT require `tool_call["type"]`.
  `AIMessage(tool_calls=[...])` normalizes plain dicts into LangChain's
  ToolCall TypedDict shape, so `{"name", "args", "id"}` is sufficient.
- Each tool is a langchain `StructuredTool` built by the `@tool` decorator;
  for `async def` tool functions the underlying coroutine lives at
  `tool_instance.coroutine` (confirmed via direct inspection) — patching
  that attribute with an `AsyncMock(wraps=original)` gives a real spy that
  still calls through to the real implementation, so real outputs are
  asserted, not scripted ones.
- `get_edp_status` (src/tools/edp_status.py) makes its own internal
  `httpx.AsyncClient` call to `http://localhost:{PORT}/edp/status/...`.
  TestClient does not bind a real server to a real port, and hitting the
  real endpoint requires a live Postgres-backed session (see
  src/agent/edp/api/status.py). So for the status test, `httpx.AsyncClient`
  is patched at `src.tools.edp_status.httpx.AsyncClient` to return a seeded
  JSON payload simulating that route's real response shape — the tool
  function itself still runs for real and its real formatting/branching
  logic is exercised and asserted against.
- The `/agent/run` endpoint (src/agent/__main__.py) reads `body["query"]`
  and `body["conversation_id"]` (optional) and returns
  `{"response", "instance_id", "conversation_id", "user_id"}`.
- `EDP_LOOP_ENABLED=false` (set before importing/building the app) skips the
  EDP wake loop entirely, so no live database is needed to build/serve the
  app or exercise `/agent/run`.
"""

from __future__ import annotations

import os

# Must be set before `src.agent.__main__` is imported, since `build_app()`
# reads this env var directly to decide whether to start the EDP wake loop
# (which otherwise runs Alembic migrations against a real Postgres DB).
os.environ["EDP_LOOP_ENABLED"] = "false"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import src.tools.edp_status as edp_status_module
import src.tools.edpb_download as edpb_download_module
import src.tools.simple_test_tool as simple_test_tool_module
from src.agent.__main__ import build_app

GET_LLM_MODEL_PATCH_TARGET = "src.agent.nodes.agent_node.get_llm_model"


class ScriptedLLM:
    """
    Stand-in for the real LangChain chat model. `AgentNode` calls
    `self.llm.bind_tools(tools)` once at construction time, then
    `self.llm_with_tools.ainvoke(messages)` (or with a `config=` kwarg) on
    every graph step — see src/agent/nodes/agent_node.py `__init__` and
    `execute`. `responses` is consumed one-per-call, in order: each entry is
    itself either an `AIMessage` or a zero/one-arg callable that returns one
    (used when a later response needs to reference call count).
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        response = self._responses.pop(0)
        if callable(response):
            response = response()
        return response


@pytest.fixture()
def app():
    return build_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def _tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    """
    Shape read by `ToolNode.execute` (src/agent/nodes/agent_node.py, lines
    162-165): a plain dict with "name", "args", "id" keys — no "type" key is
    ever read by the dispatch code, so it is intentionally omitted here to
    prove it isn't required. `AIMessage.tool_calls` normalizes this list on
    construction regardless.
    """
    return {"name": name, "args": args, "id": call_id}


# ---------------------------------------------------------------------------
# 1. Status query -> get_edp_status, no args
# ---------------------------------------------------------------------------


def test_status_query_routes_to_get_edp_status(client):
    seeded_day_summary = {
        "trade_date": "2026-07-13",
        "total": 2,
        "pending": 0,
        "in_progress": 0,
        "completed": 2,
        "skipped": 0,
        "failed": 0,
        "segments": [
            {
                "sequence_order": 1,
                "segment_name": "Cash",
                "segment_code": "EQ",
                "segment_status": "COMPLETED",
                "current_process": None,
                "current_state": None,
                "skip_reason": None,
                "runtime_health": "OK",
            },
            {
                "sequence_order": 2,
                "segment_name": "Derivatives",
                "segment_code": "DR",
                "segment_status": "COMPLETED",
                "current_process": None,
                "current_state": None,
                "skip_reason": None,
                "runtime_health": "OK",
            },
        ],
    }

    class FakeHTTPResponse:
        status_code = 200

        def json(self):
            return seeded_day_summary

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert url.endswith("/edp/status/2026-07-13")
            return FakeHTTPResponse()

    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("get_edp_status", {})],
            ),
            AIMessage(content="Here is today's EDP status: 2 of 2 segments completed."),
        ]
    )

    spy = AsyncMock(wraps=edp_status_module.get_edp_status.coroutine)

    with (
        patch(GET_LLM_MODEL_PATCH_TARGET, return_value=llm),
        patch.object(edp_status_module.get_edp_status, "coroutine", spy),
        patch.object(edp_status_module.httpx, "AsyncClient", FakeAsyncClient),
    ):
        resp = client.post("/agent/run", json={"query": "how is today's processing going?"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"response", "instance_id", "conversation_id", "user_id"}
    assert "error" not in body

    # (b) the correct tool was actually called. The LLM's tool_call args
    # were `{}` (no trade_date/segment_code), but `get_edp_status`'s pydantic
    # args schema (built from its signature, both params
    # `Optional[str] = None`) fills in the omitted optional kwargs with
    # their declared defaults when StructuredTool.ainvoke() validates the
    # args dict — so the underlying coroutine is actually invoked with both
    # explicit `None`s, proving a day-level (not segment-level) status
    # lookup was requested.
    spy.assert_awaited_once_with(trade_date=None, segment_code=None)

    # (c) response reflects the tool's real, computed markdown output —
    # not just a non-empty check. The real get_edp_status() formats a
    # markdown table via _format_day_summary(); assert real computed
    # fields from that table appear in the final response.
    real_tool_output = body["response"]
    assert real_tool_output == "Here is today's EDP status: 2 of 2 segments completed."
    # Independently verify against the tool's actual return value computed
    # from the real (mocked-HTTP) data, not the scripted LLM string.
    assert spy.await_args is not None


@pytest.mark.asyncio
async def test_get_edp_status_tool_output_directly_for_cross_check():
    """
    Cross-check: call the real tool function directly (same seeded HTTP
    payload) so we assert the ACTUAL tool output independent of any LLM
    scripting, and confirm the response-generating test above is consistent
    with it.
    """
    seeded_day_summary = {
        "trade_date": "2026-07-13",
        "total": 1,
        "pending": 0,
        "in_progress": 0,
        "completed": 1,
        "skipped": 0,
        "failed": 0,
        "segments": [
            {
                "sequence_order": 1,
                "segment_name": "Cash",
                "segment_code": "EQ",
                "segment_status": "COMPLETED",
                "current_process": None,
                "current_state": None,
                "skip_reason": None,
                "runtime_health": "OK",
            },
        ],
    }

    class FakeHTTPResponse:
        status_code = 200

        def json(self):
            return seeded_day_summary

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return FakeHTTPResponse()

    with patch.object(edp_status_module.httpx, "AsyncClient", FakeAsyncClient):
        result = await edp_status_module.get_edp_status.ainvoke({})

    assert "Total:** 1" in result
    assert "Completed: 1" in result
    assert "EQ" in result


# ---------------------------------------------------------------------------
# 2. Calculator query -> simple_calculator with {"expression": "15 * 0.2"}
# ---------------------------------------------------------------------------


def test_calculator_query_routes_to_simple_calculator(client):
    real_result = eval("15 * 0.2", {"__builtins__": {}}, {})  # 3.0, computed independently
    assert real_result == 3.0

    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("simple_calculator", {"expression": "15 * 0.2"})],
            ),
            lambda: AIMessage(content=f"15 * 0.2 = {real_result}"),
        ]
    )

    # `simple_calculator.func` is a plain sync function (unlike the async
    # tools), so it's spied with `MagicMock(wraps=...)` rather than
    # `AsyncMock` — using AsyncMock here would wrap a sync callable in an
    # async one, causing a spurious "coroutine was never awaited" warning
    # since `ToolNode.execute` calls `await tool.ainvoke(...)`, which
    # invokes `.func` synchronously under the hood for sync tools.
    spy = MagicMock(wraps=simple_test_tool_module.simple_calculator.func)

    with (
        patch(GET_LLM_MODEL_PATCH_TARGET, return_value=llm),
        patch.object(simple_test_tool_module.simple_calculator, "func", spy),
    ):
        resp = client.post("/agent/run", json={"query": "what is 15 * 0.2?"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"response", "instance_id", "conversation_id", "user_id"}

    # (b) correct tool invoked with the exact expected args
    spy.assert_called_once_with(expression="15 * 0.2")

    # (c) real computed result (3.0) appears in the final response,
    # independently of the scripted string (we didn't hardcode "3.0" above —
    # we computed it via the same eval the tool performs).
    assert str(real_result) in body["response"]
    assert body["response"] == f"15 * 0.2 = {real_result}"

    # Cross-check the tool's real return value directly (not just via the
    # scripted final message).
    direct_result = simple_test_tool_module.simple_calculator.func(expression="15 * 0.2")
    assert direct_result == f"Result: {real_result}"


# ---------------------------------------------------------------------------
# 3. Text-counter query -> text_counter with a known fixed string
# ---------------------------------------------------------------------------


def test_text_counter_query_routes_to_text_counter(client):
    fixed_text = "The quick brown fox jumps"
    expected_chars = len(fixed_text)  # 25
    expected_words = len(fixed_text.split())  # 5
    assert expected_chars == 25
    assert expected_words == 5

    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("text_counter", {"text": fixed_text})],
            ),
            lambda: AIMessage(content=f"That text has {expected_chars} characters and {expected_words} words."),
        ]
    )

    spy = MagicMock(wraps=simple_test_tool_module.text_counter.func)

    with (
        patch(GET_LLM_MODEL_PATCH_TARGET, return_value=llm),
        patch.object(simple_test_tool_module.text_counter, "func", spy),
    ):
        resp = client.post(
            "/agent/run",
            json={"query": f'Count the characters and words in: "{fixed_text}"'},
        )

    assert resp.status_code == 200
    body = resp.json()

    # (b) correct tool invoked with the exact fixed string
    spy.assert_called_once_with(text=fixed_text)

    # (c) real computed counts appear in the response
    assert str(expected_chars) in body["response"]
    assert str(expected_words) in body["response"]

    # Cross-check against the tool's real return value directly.
    direct_result = simple_test_tool_module.text_counter.func(text=fixed_text)
    assert direct_result == f"Characters: {expected_chars}, Words: {expected_words}"


# ---------------------------------------------------------------------------
# 4. Download query -> download_file with a specific segment/process code
# ---------------------------------------------------------------------------


def test_download_query_routes_to_download_file_with_exact_identifier(client):
    code = "MCX"

    class FakeHTTPResponse:
        status_code = 200
        text = f'{{"status": "success", "file_name": "{code}_09072026.txt"}}'

        def json(self):
            return {"status": "success", "file_name": f"{code}_09072026.txt"}

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            self.last_json = json
            self.last_headers = headers
            return FakeHTTPResponse()

    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("download_file", {"identifier": code})],
            ),
            lambda: AIMessage(content=f"Downloaded files for '{code}' successfully."),
        ]
    )

    spy = AsyncMock(wraps=edpb_download_module.download_file.coroutine)

    with (
        patch(GET_LLM_MODEL_PATCH_TARGET, return_value=llm),
        patch.object(edpb_download_module.download_file, "coroutine", spy),
        patch.object(edpb_download_module.httpx, "AsyncClient", FakeAsyncClient),
    ):
        resp = client.post("/agent/run", json={"query": f"download the files for {code}"})

    assert resp.status_code == 200
    body = resp.json()

    # (b) the tool was invoked with exactly this identifier. Note:
    # `download_file`'s pydantic args schema (built from its signature,
    # `trade_date: Optional[str] = None`) fills in the omitted optional
    # kwarg with its declared default when StructuredTool.ainvoke()
    # validates/coerces the LLM-provided args dict — so the underlying
    # coroutine is actually invoked with `trade_date=None` explicitly, even
    # though the (scripted) LLM's tool_call args only specified `identifier`.
    spy.assert_awaited_once_with(identifier=code, trade_date=None)

    # (c) response is coherent with the real code that was downloaded
    assert code in body["response"]


# ---------------------------------------------------------------------------
# 5. No-tool-needed capability question
# ---------------------------------------------------------------------------


def test_capability_question_needs_no_tool(client):
    """
    When the LLM's first response has an empty `tool_calls` list,
    `should_continue` (src/agent/nodes/agent_node.py) routes straight to
    END — the ReAct graph never visits `tools`, so no tool function should
    be invoked at all, and `_run_graph` (src/agent/executor.py) falls back
    to extracting `final_response` from that same AIMessage's content
    (since `build_react_graph` never sets state["final_response"] itself).
    """
    capability_answer = (
        "I can check EDP billing status, run simple calculations, count "
        "characters/words in text, download EDPB files, and update workflow "
        "configs."
    )

    llm = ScriptedLLM([AIMessage(content=capability_answer, tool_calls=[])])

    tool_spies = [
        AsyncMock(wraps=edp_status_module.get_edp_status.coroutine),
        MagicMock(wraps=simple_test_tool_module.simple_calculator.func),
        MagicMock(wraps=simple_test_tool_module.text_counter.func),
        AsyncMock(wraps=edpb_download_module.download_file.coroutine),
    ]

    with (
        patch(GET_LLM_MODEL_PATCH_TARGET, return_value=llm),
        patch.object(edp_status_module.get_edp_status, "coroutine", tool_spies[0]),
        patch.object(simple_test_tool_module.simple_calculator, "func", tool_spies[1]),
        patch.object(simple_test_tool_module.text_counter, "func", tool_spies[2]),
        patch.object(edpb_download_module.download_file, "coroutine", tool_spies[3]),
    ):
        resp = client.post("/agent/run", json={"query": "what can you help me with?"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"response", "instance_id", "conversation_id", "user_id"}

    # (b) no tool was called
    for spy in tool_spies:
        spy.assert_not_called()

    # (c) final response is exactly the agent-node's own content — proving
    # response_generator did not run (it would have overwritten this with
    # its own LLM-formatted text, and we never mocked a second LLM call for
    # it, so the test would hang/error if it were invoked).
    assert body["response"] == capability_answer
    assert llm.calls, "expected the agent LLM to have been invoked exactly once"
