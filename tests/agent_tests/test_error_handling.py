"""
Bug-hunting tests for the conversational agent's error handling.

These deliberately FORCE failures (a raising tool, a raising LLM call, a
hallucinated tool name, malformed HTTP bodies) and assert on the ACTUAL
observed behavior of POST /agent/run — not the behavior we'd like it to
have. Where the agent degrades gracefully, the test documents that. Where
it leaks an internal exception message or Python type name into the
user-facing response, the test documents THAT too (as a finding, not
something to silently normalize away).

Ground truth established by reading the source (src/agent/__main__.py,
src/agent/executor.py, src/agent/nodes/agent_node.py,
src/agent/nodes/response_generator.py, src/utils/llm_provider.py):

- POST /agent/run (src/agent/__main__.py ~L305-371) wraps its entire body in
  a single try/except Exception. On ANY unhandled exception it returns
  `{"error": str(e)}` via a plain `return` — FastAPI serializes that as
  HTTP 200 (NOT a 4xx/5xx). str(e) is often just the exception's message,
  which may not leak class name/traceback, but no attempt is made to
  redact it either.
- Empty/missing `query` short-circuits BEFORE the graph runs and returns
  `{"error": "Query is required"}`, HTTP 200 (not a 422 — the field isn't
  declared as required Pydantic model, `body` is a raw dict via Body(...)).
- executor._run_graph() (src/agent/executor.py ~L242-288) has NO try/except
  of its own around `compiled_graph.ainvoke(...)` — any exception raised
  inside any graph node propagates straight up to /agent/run's handler.
- ToolNode.execute() (src/agent/nodes/agent_node.py ~L182-218) wraps each
  individual tool call in try/except Exception and converts a raising tool
  into a ToolMessage(content=f"Error executing tool {tool_name}: {e}") — the
  graph continues and the LLM gets a chance to respond gracefully. It also
  handles a tool name that isn't registered (tools_by_name.get returns
  None) with a "Tool '{tool_name}' not found" ToolMessage — no KeyError.
- AgentNode.execute() (~L90-118), the tool-selection LLM call, has NO
  try/except around `self.llm_with_tools.ainvoke(...)` — an LLM-layer
  exception there propagates all the way to /agent/run's top-level handler.
- ResponseGeneratorNode.execute() (~L164-215) DOES wrap its LLM call in a
  bare `except Exception` and falls back to
  "I apologize, but I encountered an error generating a response." — this
  is the one layer with a genuinely graceful, generic fallback.

Run: .venv\\Scripts\\python.exe -m pytest tests/agent_tests/test_error_handling.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("EDP_LOOP_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from src.agent.__main__ import build_app

# Substrings that would indicate a raw Python exception / stack trace leaked
# into a user-facing response — a real information-disclosure concern for a
# chat UI. NOTE: response_generator's own fallback message and the tool
# error format ("Error executing tool X: ...") legitimately mention "error"
# in a controlled, generic way — those are NOT leaks by themselves. We only
# flag traceback markers, file paths, or bare Python exception class names.
LEAK_MARKERS = (
    "Traceback (most recent call last)",
    '.py", line',
    'File "',
    "ConnectionError",
    "KeyError",
    "AttributeError",
    "TypeError",
    "ValueError",
    "RuntimeError",
    "NoneType",
)


def _leaked_internals(text: str) -> list[str]:
    return [marker for marker in LEAK_MARKERS if marker in text]


@pytest.fixture()
def app():
    return build_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def _fake_tool_call_response(tool_name: str, args: dict, call_id: str = "call_1"):
    """Build an AIMessage that looks like the LLM decided to call `tool_name`."""
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": args, "id": call_id}],
    )


def _fake_final_response(text: str = "Here is my answer."):
    return AIMessage(content=text, tool_calls=[])


# ---------------------------------------------------------------------------
# 1. A tool raises an exception mid-execution.
# ---------------------------------------------------------------------------
class _ScriptedLLM:
    """Minimal stand-in for the bound-LLM object AgentNode holds. Returns a
    scripted sequence of AIMessages on successive .ainvoke() calls — first
    "call this tool", then (once the ToolMessage comes back) "final answer".
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if not self._responses:
            return _fake_final_response("(no more scripted responses)")
        return self._responses.pop(0)


def test_tool_raises_exception_is_caught_and_graph_still_responds(monkeypatch, client):
    """
    Force get_edp_status's HTTP call to raise ConnectionError (simulating the
    EDP status API being down), with the LLM scripted to call that tool then
    produce a final answer once it sees the tool's error. Per ToolNode.execute
    (agent_node.py), a raising tool is caught and turned into an error
    ToolMessage — the graph should NOT crash, and /agent/run should return a
    normal 200 with a real "response" field, not a leaked stack trace.
    """
    import src.tools.edp_status as edp_status_mod

    async def _boom(path):
        raise ConnectionError("EDP status API connection refused")

    monkeypatch.setattr(edp_status_mod, "_get", _boom)

    scripted = _ScriptedLLM(
        [
            _fake_tool_call_response("get_edp_status", {}),
            _fake_final_response(
                "I couldn't check the EDP status right now due to a connection issue. Please try again shortly."
            ),
        ]
    )

    import src.agent.nodes.agent_node as agent_node_mod

    monkeypatch.setattr(
        agent_node_mod.AgentNode,
        "__init__",
        lambda self, config, tools, tenant_id="default": (
            setattr(self, "global_config", config),
            setattr(self, "tools", tools),
            setattr(self, "tenant_id", tenant_id),
            setattr(self, "model", "gpt-4o"),
            setattr(self, "system_prompt", ""),
            setattr(self, "llm_with_tools", scripted),
            None,
        )[-1],
    )

    resp = client.post("/agent/run", json={"query": "What's the status of EDP today?"})

    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, f"Top-level handler caught something it shouldn't have: {body}"
    assert "response" in body
    leaks = _leaked_internals(body["response"])
    assert not leaks, f"Tool exception leaked into user-facing response: {leaks!r} in {body['response']!r}"


# ---------------------------------------------------------------------------
# 2a. LLM call raises inside AgentNode (tool-selection call) — NOT caught
#     locally per the source read; should propagate to /agent/run's own
#     try/except and come back as {"error": ...}, HTTP 200.
# ---------------------------------------------------------------------------
def test_agent_node_llm_raises_propagates_to_top_level_handler(monkeypatch, client):
    class _RaisingLLM:
        async def ainvoke(self, messages, config=None):
            raise TimeoutError("Azure OpenAI request timed out")

    import src.agent.nodes.agent_node as agent_node_mod

    monkeypatch.setattr(
        agent_node_mod.AgentNode,
        "__init__",
        lambda self, config, tools, tenant_id="default": (
            setattr(self, "global_config", config),
            setattr(self, "tools", tools),
            setattr(self, "tenant_id", tenant_id),
            setattr(self, "model", "gpt-4o"),
            setattr(self, "system_prompt", ""),
            setattr(self, "llm_with_tools", _RaisingLLM()),
            None,
        )[-1],
    )

    resp = client.post("/agent/run", json={"query": "Ping the agent LLM"})

    # Documented actual behavior: no try/except in AgentNode.execute or
    # executor._run_graph around this call, so it propagates to /agent/run's
    # top-level except Exception -> {"error": str(e)}, HTTP 200 (not 500).
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body, f"Expected top-level handler to catch the LLM exception, got: {body}"
    leaks = _leaked_internals(body["error"])
    assert not leaks, (
        f"LLM exception propagated with a raw exception-class name in the user-facing "
        f"error field — information disclosure: {leaks!r} in {body['error']!r}"
    )


# ---------------------------------------------------------------------------
# 2b. LLM call raises inside ResponseGeneratorNode (final-answer call) — IS
#     caught locally per the source read, falls back to a generic apology.
# ---------------------------------------------------------------------------
def test_response_generator_llm_raises_is_caught_with_generic_fallback(monkeypatch):
    """
    NOTE on setup: executor._run_graph() picks build_react_graph() (the
    AgentNode/ToolNode flow) whenever self.tools is non-empty, and falls
    back to build_graph() (QueryProcessor -> ContextRetriever ->
    ResponseGeneratorNode) only when there are NO tools at all. Since this
    deployment always has local tools registered, ResponseGeneratorNode is
    never reached via /agent/run in the real ReAct flow — it's effectively
    dead code on the happy path. To exercise it at all we force the no-tools
    branch by making get_available_tools() return [] for this test only,
    which is what actually routes execution through build_graph().
    """
    import src.agent.executor as executor_mod
    import src.agent.nodes.response_generator as response_gen_mod

    monkeypatch.setattr(executor_mod, "get_available_tools", lambda: [])

    class _RaisingLLM:
        async def ainvoke(self, messages, config=None):
            raise ConnectionError("Azure OpenAI outage: connection refused")

    def _fake_get_llm_model(*args, **kwargs):
        return _RaisingLLM()

    monkeypatch.setattr(response_gen_mod, "get_llm_model", _fake_get_llm_model)

    app = build_app()
    with TestClient(app) as client:
        resp = client.post("/agent/run", json={"query": "What is 2+2?"})

    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, f"Expected response_generator's own fallback, not a top-level error: {body}"
    assert body["response"] == ("I apologize, but I encountered an error generating a response."), (
        f"response_generator's fallback message changed or wasn't used: {body['response']!r}"
    )
    leaks = _leaked_internals(body["response"])
    assert not leaks, f"response_generator fallback leaked internals: {leaks!r}"


# ---------------------------------------------------------------------------
# 3. LLM hallucinates a tool call to an unregistered tool name.
# ---------------------------------------------------------------------------
def test_hallucinated_unregistered_tool_call_is_handled_safely(monkeypatch, client):
    """
    ToolNode.execute does `self.tools_by_name.get(tool_name)` — a miss
    returns None, handled explicitly with a "not found" ToolMessage instead
    of a KeyError. Script the agent LLM to "call" a tool that was never
    registered, then produce a final answer once it sees the not-found
    message, and confirm the whole request still completes at HTTP 200 with
    no KeyError/AttributeError leaking.
    """
    scripted = _ScriptedLLM(
        [
            _fake_tool_call_response("delete_all_data", {}),
            _fake_final_response("I don't have a way to do that, so I didn't take any action."),
        ]
    )

    import src.agent.nodes.agent_node as agent_node_mod

    monkeypatch.setattr(
        agent_node_mod.AgentNode,
        "__init__",
        lambda self, config, tools, tenant_id="default": (
            setattr(self, "global_config", config),
            setattr(self, "tools", tools),
            setattr(self, "tenant_id", tenant_id),
            setattr(self, "model", "gpt-4o"),
            setattr(self, "system_prompt", ""),
            setattr(self, "llm_with_tools", scripted),
            None,
        )[-1],
    )

    resp = client.post("/agent/run", json={"query": "Please delete everything"})

    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, f"Hallucinated tool call crashed the top-level handler: {body}"
    leaks = _leaked_internals(body["response"])
    assert not leaks, f"Unregistered-tool handling leaked internals: {leaks!r} in {body['response']!r}"


# ---------------------------------------------------------------------------
# 4. Malformed / edge-case HTTP bodies to /agent/run.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload,expected_status,note",
    [
        ({}, 200, "missing query field entirely -> handler-level {'error': ...}, NOT a 422"),
        ({"query": ""}, 200, "empty string query -> handler-level {'error': ...}"),
        ({"query": None}, 200, "null query -> falsy, same {'error': ...} path as missing/empty"),
        ({"query": "x" * 50_000}, 200, "very long query -> no length limit enforced, should reach the graph"),
    ],
)
def test_malformed_request_bodies(client, payload, expected_status, note):
    resp = client.post("/agent/run", json=payload)
    assert resp.status_code == expected_status, f"{note}: got {resp.status_code}, body={resp.text[:300]}"
    body = resp.json()
    if payload.get("query"):
        # The 50k-char case: real graph runs (real LLM call) unless creds are
        # absent, in which case it's caught by the top-level handler as an
        # {"error": ...} — either way must be HTTP 200 with valid JSON shape
        # and no leaked internals.
        assert "response" in body or "error" in body
        leaked_field = body.get("response") or body.get("error") or ""
        leaks = _leaked_internals(leaked_field)
        assert not leaks, f"{note}: leaked internals {leaks!r}"
    else:
        assert body == {"error": "Query is required"}, f"{note}: unexpected body {body}"


def test_missing_query_key_is_not_a_422_validation_error(client):
    """
    Documents the concrete finding: because the endpoint declares
    `body: dict = Body(...)` (a raw dict, not a Pydantic model with `query:
    str` required), FastAPI's request-validation layer never rejects a
    missing `query` field with a clean 422 — it's a 200 with a hand-rolled
    error dict from application code instead.
    """
    resp = client.post("/agent/run", json={"unrelated_field": "no query here"})
    assert resp.status_code == 200
    assert resp.json() == {"error": "Query is required"}


def test_completely_empty_body_is_422_because_body_itself_is_required():
    """
    Body(...) makes the BODY required (some JSON object must be posted at
    all), even though nothing inside it is schema-validated. Posting with no
    body/content-type at all should 422 from FastAPI itself, not reach
    application code.
    """
    app = build_app()
    with TestClient(app) as client:
        resp = client.post("/agent/run")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 5. conversation_id sent as a non-string type.
# ---------------------------------------------------------------------------
def test_conversation_id_as_number_is_accepted_without_validation(monkeypatch, client):
    """
    `body.get("conversation_id")` is used as-is for `thread_id` with no type
    check — a JSON number sails straight through into LangGraph's
    `configurable.thread_id`, deep into graph execution, rather than being
    rejected cleanly at the request boundary.
    """
    scripted = _ScriptedLLM([_fake_final_response("Two plus two is four.")])

    import src.agent.nodes.agent_node as agent_node_mod

    monkeypatch.setattr(
        agent_node_mod.AgentNode,
        "__init__",
        lambda self, config, tools, tenant_id="default": (
            setattr(self, "global_config", config),
            setattr(self, "tools", tools),
            setattr(self, "tenant_id", tenant_id),
            setattr(self, "model", "gpt-4o"),
            setattr(self, "system_prompt", ""),
            setattr(self, "llm_with_tools", scripted),
            None,
        )[-1],
    )

    resp = client.post("/agent/run", json={"query": "2+2?", "conversation_id": 12345})

    # No 422 from a validation layer — either it works (thread_id coerced/
    # accepted as int by LangGraph's checkpointer) or it fails deep inside
    # graph execution and surfaces as a top-level {"error": ...} at HTTP 200.
    # Either way, document exactly what happens instead of assuming.
    assert resp.status_code == 200
    body = resp.json()
    if "error" in body:
        leaks = _leaked_internals(body["error"])
        assert not leaks, f"Non-string conversation_id crash leaked internals: {leaks!r} in {body['error']!r}"
    else:
        assert body.get("conversation_id") == 12345


def test_conversation_id_as_nested_object_is_accepted_without_validation(monkeypatch, client):
    """Same as above, but with a nested dict instead of a bare number —
    still no request-layer rejection; thread_id becomes an unhashable-ish
    object that either the checkpointer chokes on (surfacing as a top-level
    {"error": ...}) or silently accepts."""
    scripted = _ScriptedLLM([_fake_final_response("Two plus two is four.")])

    import src.agent.nodes.agent_node as agent_node_mod

    monkeypatch.setattr(
        agent_node_mod.AgentNode,
        "__init__",
        lambda self, config, tools, tenant_id="default": (
            setattr(self, "global_config", config),
            setattr(self, "tools", tools),
            setattr(self, "tenant_id", tenant_id),
            setattr(self, "model", "gpt-4o"),
            setattr(self, "system_prompt", ""),
            setattr(self, "llm_with_tools", scripted),
            None,
        )[-1],
    )

    resp = client.post(
        "/agent/run",
        json={"query": "2+2?", "conversation_id": {"nested": "object"}},
    )

    assert resp.status_code == 200
    body = resp.json()
    if "error" in body:
        leaks = _leaked_internals(body["error"])
        assert not leaks, f"Nested-object conversation_id crash leaked internals: {leaks!r} in {body['error']!r}"
    else:
        assert body.get("conversation_id") == {"nested": "object"}
