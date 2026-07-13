"""
REAL, LIVE end-to-end tests against the REAL agent + REAL LLM (no mocking of
the LLM or any tool). These hit `POST /agent/run` on the real FastAPI app
(built via `src.agent.__main__.build_app`) and exercise the actual LangGraph
ReAct loop with a real OpenAI-backed model, asserting on the CORRECTNESS of
computed values in the response text — not just "a tool was invoked".

Contrast with `test_tool_routing.py`, which mocks the LLM to deterministically
test routing/dispatch mechanics. These tests are intentionally slow,
non-deterministic in phrasing (but not in the underlying facts asserted), and
cost real API tokens — hence gated behind RUN_LIVE_AGENT_TESTS.

Key facts (see test_tool_routing.py docstring for full detail):
- `/agent/run` accepts `{"query": ..., "conversation_id": <optional>}` and
  returns `{"response", "instance_id", "conversation_id", "user_id"}`.
- `simple_calculator(expression)` evaluates a Python expression and returns
  `f"Result: {result}"`.
- `text_counter(text)` returns `f"Characters: {char_count}, Words: {word_count}"`.
- `download_file(filename, trade_date=None)` (src/tools/edpb_download.py) does
  NOT read a file from disk — it POSTs to a configured EDPB HTTP API
  (default/placeholder `http://localhost:9300/api/edpb/download`, per
  `src/config/agent_config.json` -> `agent_config.secrets.edpb_download`).
  Since no such server runs in this test environment, the real call fails to
  connect and the tool returns a clean, honest
  "Failed to call the EDPB download API for '<filename>': <exc>" message
  (src/tools/edpb_download.py lines 96-98) — never a fabricated success. This
  makes it safe to test the "file doesn't exist" scenario without seeding any
  fixture file on disk (there is no disk-based lookup to seed).
- `EDP_LOOP_ENABLED=false` (set before importing `src.agent.__main__`) skips
  the EDP wake loop so no live Postgres is needed to build/serve the app.
"""

import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_LIVE_AGENT_TESTS"),
    reason="Hits the real LLM (costs money, non-deterministic) — set RUN_LIVE_AGENT_TESTS=1 to run.",
)

# Must be set before `src.agent.__main__` is imported, since `build_app()`
# reads this env var directly to decide whether to start the EDP wake loop
# (which otherwise runs Alembic migrations against a real Postgres DB).
os.environ["EDP_LOOP_ENABLED"] = "false"

from fastapi.testclient import TestClient

from src.agent.__main__ import build_app

# NOTE on which real LLM this hits: the checked-in src/config/agent_config.json
# sets "llm_provider": "google" with secrets.litellm.enabled = true and a
# gateway base_url (cams-litellm.dev.cams.covasant.io) — reaching it requires
# the CAMS VPN/internal network. The plain OPENAI_API_KEY/GOOGLE_API_KEY in
# .env are placeholders, not real credentials; the LiteLLM gateway's own
# api_key (agent_config.json -> secrets.litellm.api_key) is what actually
# authenticates. Run these from a network that can reach the gateway.


@pytest.fixture()
def app():
    return build_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Arithmetic correctness
# ---------------------------------------------------------------------------

def test_live_percentage_calculation_is_correct(client):
    resp = client.post("/agent/run", json={"query": "what's 15% of 2400?"})
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    print("\n--- LIVE percentage response ---")
    print(body["response"])

    # 15% of 2400 = 360, computed independently in this test (not hardcoded
    # from a guess): 2400 * 0.15 == 360
    expected = 2400 * 0.15
    assert expected == 360
    assert "360" in body["response"]


def test_live_split_calculation_is_correct(client):
    resp = client.post(
        "/agent/run",
        json={"query": "if I have 45000 and split it 3 ways, how much is each share?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    print("\n--- LIVE split-share response ---")
    print(body["response"])

    expected = 45000 / 3
    assert expected == 15000
    response_text = body["response"].replace(",", "")
    assert "15000" in response_text


# ---------------------------------------------------------------------------
# 2. Text counting correctness (word count + char count computed via real
#    Python len()/split() on the exact same string sent to the agent).
# ---------------------------------------------------------------------------

def test_live_text_counter_is_correct(client):
    phrase = "the quick brown fox jumps over the lazy dog"
    expected_chars = len(phrase)
    expected_words = len(phrase.split())

    resp = client.post(
        "/agent/run",
        json={
            "query": (
                f"how many characters and words are in the phrase "
                f"'{phrase}'?"
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    print("\n--- LIVE text-counter response ---")
    print(f"expected_chars={expected_chars} expected_words={expected_words}")
    print(body["response"])

    assert str(expected_chars) in body["response"]
    assert str(expected_words) in body["response"]


# ---------------------------------------------------------------------------
# 3. Download of a nonexistent file — must not fabricate success
# ---------------------------------------------------------------------------

def test_live_download_nonexistent_file_reports_failure(client):
    filename = "VN_DOES_NOT_EXIST_12345.txt"
    resp = client.post(
        "/agent/run", json={"query": f"download the script named {filename}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    print("\n--- LIVE nonexistent-file download response ---")
    print(body["response"])

    response_lower = body["response"].lower()

    # Must NOT fabricate a success claim.
    assert "downloaded successfully" not in response_lower
    assert "successfully downloaded" not in response_lower

    # Must clearly communicate failure/non-existence in some honest form:
    # either an explicit "not found"-style message, or (since the configured
    # EDPB API endpoint isn't a live server in this test env) an honest
    # connection/HTTP failure message from the tool itself.
    failure_indicators = [
        "not found",
        "doesn't exist",
        "does not exist",
        "could not",
        "couldn't",
        "unable to",
        "failed",
        "error",
        "no such file",
    ]
    assert any(indicator in response_lower for indicator in failure_indicators), (
        f"Expected a clear failure/not-found indication, got: {body['response']}"
    )


# ---------------------------------------------------------------------------
# 4. Malformed/ambiguous calculation request — graceful handling, no crash,
#    no fabricated numeric answer.
# ---------------------------------------------------------------------------

def test_live_malformed_calculation_handled_gracefully(client):
    resp = client.post(
        "/agent/run", json={"query": "what's the square root of banana?"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    print("\n--- LIVE malformed-calculation response ---")
    print(body["response"])

    response_text = body["response"]
    assert response_text and response_text != "No response generated"

    # It must not present a fabricated numeric "answer" to a nonsensical
    # square root as if it were a valid, computed result. We can't assert
    # the total absence of any digit (the agent may legitimately explain
    # things using numbers), but it must not claim something like
    # "the square root of banana is <number>" — check no bare "= <number>"
    # or "is <number>" pattern is used to state a fabricated result immediately
    # following the word "banana"/"square root".
    import re
    fabricated_answer_pattern = re.compile(
        r"square root of banana (is|=|equals)\s*\d", re.IGNORECASE
    )
    assert not fabricated_answer_pattern.search(response_text), (
        f"Agent appears to have fabricated a numeric answer: {response_text}"
    )


# ---------------------------------------------------------------------------
# 5. Compound request needing two tools in a single message
# ---------------------------------------------------------------------------

def test_live_compound_request_uses_both_tools_correctly(client):
    phrase = "hello world"
    expected_words = len(phrase.split())
    assert expected_words == 2

    resp = client.post(
        "/agent/run",
        json={
            "query": (
                f"count the words in '{phrase}' and also tell me what 12 "
                f"times 12 is"
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    print("\n--- LIVE compound-request response ---")
    print(body["response"])

    expected_product = 12 * 12
    assert expected_product == 144

    assert str(expected_words) in body["response"]
    assert str(expected_product) in body["response"]
