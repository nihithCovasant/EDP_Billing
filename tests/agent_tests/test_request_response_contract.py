"""
HTTP request/response CONTRACT tests for POST /agent/run.

These tests exercise the real FastAPI app (src.agent.__main__.build_app()) via
FastAPI's TestClient, validating the exact response shape that the real
frontend (chat_ui/app.js::sendMessage()) depends on:

    - data.error           (chat_ui/app.js line ~392: `if (data.error)`)
    - data.conversation_id (chat_ui/app.js line ~397: stored in sessionStorage)
    - data.response         (chat_ui/app.js line ~402: rendered as markdown)

The only thing mocked is the LLM call itself (src.utils.llm_provider.
get_llm_model, as imported into src.agent.nodes.agent_node — the node that
executor.AgentExecutor.build_react_graph() actually wires up, since this repo
has tools registered so the ReAct graph — not the plain response_generator
graph — is what /agent/run drives). Everything else (FastAPI app, executor,
LangGraph wiring, CORS middleware, thread-id generation) is real.

Run with:
    .venv\\Scripts\\python.exe -m pytest tests/agent_tests/test_request_response_contract.py -v
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from src.agent.__main__ import build_app

# ---------------------------------------------------------------------------
# Fake LLM — mimics the "no tool call needed, just answer" path.
# AgentNode (src/agent/nodes/agent_node.py) calls:
#     self.llm = get_llm_model(...)
#     self.llm_with_tools = self.llm.bind_tools(self.tools)   # if tools exist
#     response = await self.llm_with_tools.ainvoke(messages)
# so the fake needs to support .bind_tools() (returning something with
# .ainvoke()) and .ainvoke() itself, matching a scripted, content-only
# AIMessage response with no tool_calls -- i.e. should_continue() routes
# straight to "end" instead of "tools".
# ---------------------------------------------------------------------------


class _FakeNoToolLLM:
    """Scripted LLM stand-in: always returns a fixed content-only AIMessage."""

    def __init__(self, content: str = "Hello! How can I help with EDP billing today?"):
        self._content = content

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        return AIMessage(content=self._content)


class _FakeRaisingLLM:
    """Scripted LLM stand-in that raises, to force the endpoint's error path."""

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        raise RuntimeError("simulated LLM failure")


# A realistic "get_edp_status" style day-summary response, built to match the
# exact markdown shape emitted by src/tools/edp_status.py::_format_day_summary
# (heading via "###", a summary line, then a "|"-delimited table with a
# separator row) -- what app.js's renderMarkdown() must be able to handle.
_MARKDOWN_TOOL_STYLE_RESPONSE = "\n".join(
    [
        "### \U0001f4c5 EDP Status — 2026-07-13",
        "",
        "**Total:** 5  |  \U0001f553 Pending: 1  |  ⏳ In progress: 1  |  "
        "✅ Completed: 2  |  ⏭️ Skipped: 0  |  ❌ Failed: 1",
        "",
        "| # | Segment | Status | Current step | Notes |",
        "|---|---------|--------|--------------|-------|",
        "| 1 | Equity (EQ) | ✅ COMPLETED | Done |  |",
        "| 2 | Derivatives (FO) | ❌ FAILED | Retry pending | STALE — no heartbeat recently |",
        "| 3 | Currency (CD) | ⏳ IN_PROGRESS | Uploading |  |",
    ]
)


def _make_client(fake_llm_factory) -> TestClient:
    """
    Build the real app with get_llm_model patched at the exact import site
    AgentNode uses (src.agent.nodes.agent_node.get_llm_model), then wrap it
    in a TestClient. A fresh app/client per test avoids any cross-test graph
    caching in AgentExecutor._compiled_graphs.
    """
    patcher = patch(
        "src.agent.nodes.agent_node.get_llm_model",
        side_effect=lambda *a, **k: fake_llm_factory(),
    )
    patcher.start()
    app = build_app()
    client = TestClient(app)
    client._contract_test_patcher = patcher  # stash so callers can stop it
    return client


@pytest.fixture
def success_client():
    client = _make_client(_FakeNoToolLLM)
    yield client
    client._contract_test_patcher.stop()


@pytest.fixture
def error_client():
    client = _make_client(_FakeRaisingLLM)
    yield client
    client._contract_test_patcher.stop()


@pytest.fixture
def markdown_client():
    client = _make_client(lambda: _FakeNoToolLLM(_MARKDOWN_TOOL_STYLE_RESPONSE))
    yield client
    client._contract_test_patcher.stop()


# ---------------------------------------------------------------------------
# 1. Successful response shape: response + conversation_id, correct casing.
# ---------------------------------------------------------------------------


def test_success_response_has_response_and_conversation_id(success_client: TestClient):
    resp = success_client.post("/agent/run", json={"query": "hello"})
    assert resp.status_code == 200

    data = resp.json()

    # Fields app.js's sendMessage() actually reads.
    assert "response" in data
    assert isinstance(data["response"], str)
    assert data["response"].strip() != ""

    assert "conversation_id" in data
    assert isinstance(data["conversation_id"], str)
    assert data["conversation_id"].strip() != ""

    # No error field should be present on a success response.
    assert "error" not in data

    # Explicitly rule out camelCase field-name drift that would silently
    # break app.js (it reads snake_case only).
    assert "conversationId" not in data
    assert "responseText" not in data


# ---------------------------------------------------------------------------
# 2. Error response shape: data.error present, no misleading `response`.
# ---------------------------------------------------------------------------


def test_error_response_has_error_field_and_no_misleading_response(error_client: TestClient):
    resp = error_client.post("/agent/run", json={"query": "hello"})
    assert resp.status_code == 200  # endpoint always returns 200; error is in-body

    data = resp.json()

    assert "error" in data
    assert isinstance(data["error"], str)
    assert data["error"].strip() != ""
    assert "simulated LLM failure" in data["error"]

    # app.js checks `if (data.error)` first and returns early -- so a
    # `response` key must not be present (the endpoint's except-branch in
    # src/agent/__main__.py returns only {"error": str(e)}, no other keys).
    assert "response" not in data
    assert "conversation_id" not in data


# ---------------------------------------------------------------------------
# 3. Content-Type header.
# ---------------------------------------------------------------------------


def test_response_content_type_is_json(success_client: TestClient):
    resp = success_client.post("/agent/run", json={"query": "hello"})
    content_type = resp.headers.get("content-type", "")
    assert content_type.startswith("application/json")


# ---------------------------------------------------------------------------
# 4. CORS behaviour.
#
# FINDING: build_app() in src/agent/__main__.py DOES register CORSMiddleware
# with an explicit allow_origins list (localhost:3000/3001/8000/5173, the
# aifabric-frontend dev host, and their 127.0.0.1 equivalents), allow_credentials
# =True. This means the app.js comment "no CORS setup needed" is only true for
# the same-origin case app.js itself uses (no Origin header sent by same-
# origin fetch calls) -- it is NOT true in general: a cross-origin caller from
# one of the allow-listed dev frontends *does* get CORS headers.
#
# So the concrete, verified behaviour is:
#   - No Origin header (same-origin, matches how app.js calls the API):
#     no Access-Control-Allow-Origin header at all.
#   - Origin header from an allow-listed dev origin: Access-Control-Allow-
#     Origin IS present and echoes that origin.
# ---------------------------------------------------------------------------


def test_cors_header_absent_without_origin_header(success_client: TestClient):
    """Same-origin call (as app.js makes it, no Origin header) gets no CORS headers."""
    resp = success_client.post("/agent/run", json={"query": "hello"})
    assert "access-control-allow-origin" not in resp.headers


def test_cors_header_present_for_allowlisted_origin(success_client: TestClient):
    """
    Contract mismatch vs. the app.js comment: CORSMiddleware IS configured
    in build_app() with a concrete allow_origins list. A request carrying an
    Origin header that's on that list gets Access-Control-Allow-Origin back.
    """
    resp = success_client.post(
        "/agent/run",
        json={"query": "hello"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_header_absent_for_non_allowlisted_origin(success_client: TestClient):
    """An Origin not on the allow-list does not get Access-Control-Allow-Origin back."""
    resp = success_client.post(
        "/agent/run",
        json={"query": "hello"},
        headers={"Origin": "http://evil.example.com"},
    )
    assert "access-control-allow-origin" not in resp.headers


# ---------------------------------------------------------------------------
# 5. Randomized conversation_id generation (no conversation_id in request).
# ---------------------------------------------------------------------------


def test_two_requests_without_conversation_id_get_different_ids(success_client: TestClient):
    resp1 = success_client.post("/agent/run", json={"query": "hello"})
    resp2 = success_client.post("/agent/run", json={"query": "hello again"})

    id1 = resp1.json()["conversation_id"]
    id2 = resp2.json()["conversation_id"]

    assert id1 != id2


# ---------------------------------------------------------------------------
# 6. Markdown well-formedness for a tool-response-shaped answer, checked
#    against what app.js's renderMarkdown() actually parses (see
#    inlineFormat()'s "**bold**" regex and the "|"-row table parser).
# ---------------------------------------------------------------------------


def test_markdown_tool_response_is_well_formed_for_frontend_renderer(markdown_client: TestClient):
    resp = markdown_client.post("/agent/run", json={"query": "How is today's EDP processing going?"})
    assert resp.status_code == 200

    data = resp.json()
    text = data["response"]
    assert text.strip() != ""

    # (a) Bold markers "**...**" must be balanced -- an odd count means
    # inlineFormat()'s regex (/\*\*(.+?)\*\*/g) will leave a stray "**" in
    # the rendered HTML instead of turning it into <strong>.
    bold_marker_count = text.count("**")
    assert bold_marker_count % 2 == 0, (
        f"Unbalanced '**' bold markers in tool response (count={bold_marker_count}); "
        "app.js's renderMarkdown() would render a stray '**' instead of <strong>."
    )

    # (b) Any markdown table (rows starting with "|") must have a consistent
    # column count within that table, matching app.js's splitRow()/table
    # parser expectations (header cells and each data row split on "|").
    lines = text.replace("\r\n", "\n").split("\n")
    table_row_lines = [ln for ln in lines if ln.strip().startswith("|")]
    assert table_row_lines, "expected at least one markdown table row in a status-style response"

    def split_row(line: str):
        trimmed = line.strip()
        trimmed = re.sub(r"^\|", "", trimmed)
        trimmed = re.sub(r"\|$", "", trimmed)
        return [cell.strip() for cell in trimmed.split("|")]

    column_counts = {len(split_row(ln)) for ln in table_row_lines}
    assert len(column_counts) == 1, (
        f"Inconsistent column counts across table rows: {column_counts}; "
        "app.js's renderMarkdown() zips header cells to each row 1:1 and would "
        "misrender ragged rows."
    )

    # Sanity: the separator row ("|---|---|...") must have the same column
    # count as the header, exactly what app.js's isTableSeparator()/splitRow()
    # pairing assumes.
    separator_lines = [ln for ln in table_row_lines if re.match(r"^\s*\|?[\s:|-]+\|?\s*$", ln) and "-" in ln]
    assert separator_lines, "expected a '|---|---|' style separator row directly under the header"
