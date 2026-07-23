"""
Regression test for a bug hit after switching the Google provider from the
LiteLLM gateway to the direct Gemini SDK (src/utils/llm_provider.py): unlike
OpenAI-compatible responses, `ChatGoogleGenerativeAI` can return
`AIMessage.content` as a *list* of content blocks (e.g.
[{"type": "text", "text": "..."}]) instead of a plain string.

`AgentExecutor._run_graph` (src/agent/executor.py) used to assign that raw
list straight into `final_state["final_response"]` when falling back to the
last AI message (the `build_react_graph` path, which is what `/agent/run`
always uses here since real tools are always loaded — see
test_tool_routing.py's module docstring for that established fact). The
`/agent/run` JSON response then carried `"response"` as a raw array, and the
chat UI's markdown renderer (chat_ui/app.js `renderMarkdown`) called
`raw.replace(...)` on it, crashing with "raw.replace is not a function".

Fixed via `_stringify_message_content()` in src/agent/executor.py, which
flattens list-of-content-block responses into plain text before they ever
reach `final_response`. This test proves the fix end-to-end through the
real `/agent/run` endpoint (only the LLM call itself is scripted).
"""

from __future__ import annotations

import os

os.environ["EDP_LOOP_ENABLED"] = "false"

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from src.agent.__main__ import build_app
from tests.agent_tests.test_tool_routing import GET_LLM_MODEL_PATCH_TARGET, ScriptedLLM


@pytest.fixture()
def client():
    with TestClient(build_app()) as c:
        yield c


def test_gemini_style_list_content_final_answer_is_flattened_to_a_string(client):
    """No tool call at all -- straight to a final answer, but shaped the way
    the real Gemini SDK shapes it (a list of content blocks), not a plain
    string like OpenAI/LiteLLM would return."""
    llm = ScriptedLLM(
        [
            AIMessage(
                content=[
                    {"type": "text", "text": "Sure, I can help with that. "},
                    {"type": "text", "text": "What would you like to name this config?"},
                ]
            ),
        ]
    )

    with patch(GET_LLM_MODEL_PATCH_TARGET, return_value=llm):
        resp = client.post("/agent/run", json={"query": "Upload a new workflow config"})

    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    assert isinstance(body["response"], str), (
        f"response must be a plain string for the chat UI's markdown renderer, "
        f"got {type(body['response'])}: {body['response']!r}"
    )
    assert body["response"] == "Sure, I can help with that. What would you like to name this config?"


def test_gemini_style_list_content_after_a_tool_call_is_also_flattened(client):
    """Same shape bug, but on the 2nd (post-tool-call) LLM turn, which is the
    path _run_graph's fallback-extraction branch actually hits in practice."""
    import src.tools.edp_status as edp_status_module

    async def fake_get(path):
        return 200, {"trade_date": "2026-07-15", "carried_forward": False, "version_name": None}

    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "list_edp_workflow_versions", "args": {}, "id": "call_1"}],
            ),
            AIMessage(content=[{"type": "text", "text": "No saved workflow versions yet."}]),
        ]
    )

    with patch(GET_LLM_MODEL_PATCH_TARGET, return_value=llm), patch.object(edp_status_module, "_get", fake_get):
        resp = client.post("/agent/run", json={"query": "what versions have I saved?"})

    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    assert isinstance(body["response"], str)
    assert body["response"] == "No saved workflow versions yet."
