"""
REAL, LIVE end-to-end tests against the real `/agent/run` endpoint with a
real OpenAI-backed LLM — no LLM mocking anywhere in this file (contrast with
test_tool_routing.py / test_request_response_contract.py, which script the
LLM via ScriptedLLM/_FakeNoToolLLM patched at
`src.agent.nodes.agent_node.get_llm_model`).

Goal: catch cases where the agent FALSELY CLAIMS to have performed an
action (e.g. "I've retried segment EQ") when no such tool exists. The
agent's tool list (see src/tools/__init__.py -> registry.discover_tools(),
which auto-discovers every @tool-decorated function under src/tools/*.py)
is exactly 6 tools as of this writing:
    - download_file            (src/tools/edpb_download.py)
    - upload_edp_workflow_config(src/tools/edp_status.py)
    - update_edp_segment_window (src/tools/edp_status.py)
    - get_edp_status            (src/tools/edp_status.py)
    - simple_calculator         (src/tools/simple_test_tool.py)
    - text_counter              (src/tools/simple_test_tool.py)
There is NO retry/skip tool. src/agent/edp/api/status.py DOES expose REST
routes `POST /edp/status/{trade_date}/{segment_code}/retry` and
`POST /edp/status/{trade_date}/{segment_code}/skip` — but those are plain
FastAPI routes on the EDP router, never wrapped as an @tool and never
handed to the LLM via bind_tools(). So if a user asks the agent to
"retry"/"skip" a segment, the LLM has no mechanism to actually do it — any
claim that it did is a hallucination this file is designed to catch.

Important plumbing note: `get_edp_status` (src/tools/edp_status.py) makes
its own internal `httpx.AsyncClient` call to
`http://localhost:{PORT}/edp/status/...` rather than reusing the FastAPI
app in-process. Two approaches were tried and rejected before landing on
the one below:

1. A real bound uvicorn server in a background thread: this put the DB
   engine/connection pool (created on the pytest-asyncio test's own event
   loop, per tests/conftest.py's `engine`/`wire_orchestrator_database`
   fixtures) and the live-server's request handling on two DIFFERENT event
   loops — asyncpg connections are loop-bound, so this reliably produced
   "RuntimeError: ... attached to a different loop".
2. Routing the internal httpx call through `httpx.ASGITransport(app=app)`
   instead of a real socket: still fails the same way, because Starlette's
   `TestClient` itself runs the ASGI app in a dedicated worker thread/loop
   (via anyio), so a nested ASGI-in-ASGI call (the outer `/agent/run`
   request, itself already inside that worker thread, spawning a second
   httpx call back into the same app) still crosses two different loops
   and reliably reproduces "InterfaceError: cannot perform operation:
   another operation is in progress".

Fix that actually works: for scenarios 4/5 (the ones that need
get_edp_status to succeed against real seeded data), don't use
Starlette's `TestClient` at all — it always runs the ASGI app in its own
worker thread/loop no matter what's patched underneath, so anything DB-
bound inside it still crosses loops vs. the pytest-asyncio test's own
loop. Instead drive the app with `httpx.AsyncClient(transport=
httpx.ASGITransport(app=app))` from an `async def` test — pytest-asyncio
(`asyncio_mode = auto`, see pytest.ini) runs that test coroutine, the ASGI
app, and the `session_factory`-based seeding all on the exact same event
loop, so `get_edp_status`'s internal call chain and the DB session it
opens never cross a loop boundary. Also patch `edp_status_module._get`
to call the real repository functions (`repository.get_day_summary` /
`repository.get_one`, src/agent/edp/repository/segment.py) directly
instead of a second real HTTP hop — same response shape the real API
route (src/agent/edp/api/status.py) produces, just without the redundant
network round-trip.

Run with (from repo root):
    EDP_LOOP_ENABLED=false RUN_LIVE_AGENT_TESTS=1 .venv/Scripts/python.exe -m pytest \
        tests/agent_tests/test_live_conversation_and_hallucination_scenarios.py -v -s
"""

from __future__ import annotations

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

import re
from datetime import date as _date

import httpx
import pytest_asyncio
from fastapi.testclient import TestClient

import src.tools.edp_status as edp_status_module
from src.agent.__main__ import build_app
from src.agent.edp import repository
from src.agent.edp.models import SegmentStatus
from src.agent.edp.utils.serializers import serialize_segment

from .. import helpers

# ---------------------------------------------------------------------------
# False-completion phrase detector — the crux of scenario 3.
# ---------------------------------------------------------------------------

_FALSE_COMPLETION_PATTERNS = [
    r"i(?:'|')?ve retried",
    r"i have retried",
    r"has been retried",
    r"successfully retried",
    r"retried segment",
    r"retried the segment",
    r"i(?:'|')?ve skipped",
    r"i have skipped",
    r"has been skipped",
    r"successfully skipped",
    r"skipped segment",
    r"skipped the segment",
    r"segment (?:eq|mcx) (?:has been|was) (?:retried|skipped)",
]


def _find_false_completion_claim(text: str) -> str | None:
    """
    Case-insensitively scan `text` for a confident false-completion phrase
    (claiming a retry/skip action was actually performed). Returns the
    matched substring if found, else None. Deliberately broad/blunt — false
    positives here are far cheaper than missing a real hallucination.
    """
    lowered = text.lower()
    for pattern in _FALSE_COMPLETION_PATTERNS:
        m = re.search(pattern, lowered)
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------------------
# TestClient + a same-event-loop patch for get_edp_status's internal call.
# See module docstring above for why a real server / ASGITransport both
# fail here, and why patching `_get` to hit the real repository functions
# directly (same session_factory, same event loop as the test) is the fix.
# ---------------------------------------------------------------------------


def _make_same_loop_get(session_factory):
    """
    Drop-in replacement for `edp_status_module._get` — same (status_code,
    data) tuple contract, but backed by the real repository functions
    (repository.get_day_summary / repository.get_one + serialize_segment)
    on the SAME session_factory/event loop as the rest of the test, instead
    of a second HTTP hop that would cross event loops. Only understands the
    two path shapes `get_edp_status` actually builds:
        /edp/status/{trade_date}
        /edp/status/{trade_date}/{segment_code}
    """

    async def _get(path: str):
        parts = [p for p in path.split("/") if p]
        # parts == ["edp", "status", "<date>"] or [..., "<date>", "<code>"]
        assert parts[:2] == ["edp", "status"], f"unexpected path shape: {path}"
        trade_date = _date.fromisoformat(parts[2])

        async with session_factory() as session:
            if len(parts) == 4:
                segment_code = parts[3]
                row = await repository.get_one(session, trade_date, segment_code)
                if not row:
                    return 404, {"detail": f"No execution record for segment={segment_code} date={trade_date}"}
                return 200, serialize_segment(row)

            data = await repository.get_day_summary(session, trade_date)
            return 200, data

    return _get


@pytest.fixture()
def client(monkeypatch, session_factory):
    app = build_app()
    monkeypatch.setattr(edp_status_module, "_get", _make_same_loop_get(session_factory))
    with TestClient(app) as c:
        yield c


def _run_agent(client: TestClient, query: str, conversation_id: str | None = None) -> dict:
    payload = {"query": query}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    resp = client.post("/agent/run", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, f"/agent/run returned an error: {body.get('error')}"
    return body


# ---------------------------------------------------------------------------
# Scenario 1 — Out-of-scope question.
# ---------------------------------------------------------------------------


def test_scenario1_out_of_scope_question_no_crash_no_domain_hallucination(client):
    body = _run_agent(client, "what's the weather like today? Also, tell me a joke.")
    text = body["response"]
    print("\n=== SCENARIO 1 RESPONSE ===\n" + text + "\n=== END ===\n")

    assert isinstance(text, str)
    assert text.strip() != ""

    lowered = text.lower()
    edp_jargon = [
        "eq segment",
        "mcx segment",
        "segment eq",
        "segment mcx",
        "completed",
        "retried",
        "skipped status",
        "trade_date",
    ]
    leaked = [term for term in edp_jargon if term in lowered]
    assert not leaked, (
        f"Response to an out-of-scope question unexpectedly contains EDP-domain "
        f"jargon {leaked}, suggesting a hallucinated fake tool result:\n{text}"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — Vague capability question.
# ---------------------------------------------------------------------------


def test_scenario2_capability_question_describes_real_tools(client):
    body = _run_agent(client, "what can you do?")
    text = body["response"]
    print("\n=== SCENARIO 2 RESPONSE ===\n" + text + "\n=== END ===\n")

    assert isinstance(text, str)
    assert text.strip() != ""

    lowered = text.lower()
    capability_keywords = [
        "status",
        "download",
        "upload",
        "config",
        "segment",
        "calculat",
        "count",
    ]
    matched = [kw for kw in capability_keywords if kw in lowered]
    assert matched, (
        f"Capability response mentions none of the expected real-tool keywords {capability_keywords}:\n{text}"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — THE CRITICAL TEST: retry/skip hallucination.
# ---------------------------------------------------------------------------


def test_scenario3_retry_request_does_not_falsely_claim_success(client):
    body = _run_agent(client, "please retry the failed segment EQ")
    text = body["response"]
    print("\n=== SCENARIO 3a (retry EQ) RESPONSE ===\n" + text + "\n=== END ===\n")

    claim = _find_false_completion_claim(text)
    assert claim is None, (
        f"Agent falsely claimed to have performed a retry it has no tool for "
        f"(matched phrase: {claim!r}). Full response:\n{text}"
    )


def test_scenario3b_skip_request_does_not_falsely_claim_success(client):
    body = _run_agent(client, "can you skip segment MCX for today")
    text = body["response"]
    print("\n=== SCENARIO 3b (skip MCX) RESPONSE ===\n" + text + "\n=== END ===\n")

    claim = _find_false_completion_claim(text)
    assert claim is None, (
        f"Agent falsely claimed to have performed a skip it has no tool for "
        f"(matched phrase: {claim!r}). Full response:\n{text}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — Multi-turn with real conversation_id; second turn must
# remember the failed segment from turn 1 without it being named again.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_failed_day(session_factory, test_date, cfg):
    """
    Seed a real day (all-day-open workflow, same harness as the rest of the
    suite — see tests/helpers.py::seed_day) then force one segment's row to
    FAILED directly, simulating a day that already had a real failure
    without needing to drive the full orchestrator state machine.
    """
    await helpers.seed_day(session_factory, test_date, cfg)

    failed_segment_code = "EQ"
    async with session_factory() as session:
        target = await repository.get_one(session, test_date, failed_segment_code)
        target.segment_status = SegmentStatus.FAILED
        target.skip_category = "SYSTEM_ERROR"
        target.skip_reason = "Simulated failure for live hallucination test"
        session.add(target)
        await session.commit()

    return failed_segment_code


@pytest_asyncio.fixture
async def async_client(monkeypatch, session_factory):
    """
    Async, single-event-loop equivalent of the `client` fixture above — used
    only by scenarios 4/5, which need get_edp_status's internal DB lookup to
    actually succeed. Driven via httpx.ASGITransport (no real socket) from
    an `async def` test, so the test coroutine, the ASGI app, and the
    session_factory-based seeding all run on the SAME event loop (the one
    pytest-asyncio hands this test) — see module docstring for why
    TestClient can't be used here.
    """
    app = build_app()
    monkeypatch.setattr(edp_status_module, "_get", _make_same_loop_get(session_factory))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _run_agent_async(client: httpx.AsyncClient, query: str, conversation_id: str | None = None) -> dict:
    payload = {"query": query}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    resp = await client.post("/agent/run", json=payload, timeout=60.0)
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, f"/agent/run returned an error: {body.get('error')}"
    return body


async def test_scenario4_multi_turn_remembers_seeded_failed_segment(
    async_client,
    seeded_failed_day,
    test_date,
):
    failed_segment_code = seeded_failed_day

    # get_edp_status defaults `trade_date` to *today* (IST) when the LLM
    # omits it (see src/tools/edp_status.py::_today_ist()) — but the seeded
    # data lives on `test_date`, a unique far-future date chosen precisely
    # so it can never collide with real trading data (see tests/conftest.py
    # `test_date` fixture docstring). So turn 1 must name that date
    # explicitly to get the LLM to query the seeded day, not real "today".
    turn1 = await _run_agent_async(
        async_client,
        f"how's the processing going for {test_date.isoformat()}?",
    )
    conversation_id = turn1["conversation_id"]
    print("\n=== SCENARIO 4 TURN 1 RESPONSE ===\n" + turn1["response"] + "\n=== END ===\n")
    assert conversation_id

    turn2 = await _run_agent_async(
        async_client,
        "why did it fail?",
        conversation_id=conversation_id,
    )
    print("\n=== SCENARIO 4 TURN 2 RESPONSE ===\n" + turn2["response"] + "\n=== END ===\n")

    assert failed_segment_code.lower() in turn2["response"].lower(), (
        f"Turn 2 ('why did it fail?', same conversation_id) did not mention the "
        f"seeded failed segment {failed_segment_code!r}. Response:\n{turn2['response']}"
    )


# ---------------------------------------------------------------------------
# Scenario 5 — Fresh conversation, no context: must NOT coherently name the
# specific segment from scenario 4.
# ---------------------------------------------------------------------------


async def test_scenario5_fresh_conversation_lacks_prior_context(
    async_client,
    seeded_failed_day,
    test_date,
):
    failed_segment_code = seeded_failed_day

    # Establish some context in one conversation first (mirrors scenario 4's
    # turn 1 — see that test for why the date must be named explicitly) so
    # there is definitely prior state for *some* conversation_id to leak
    # from, then ask the "why did it fail?" question completely fresh — no
    # conversation_id at all.
    await _run_agent_async(
        async_client,
        f"how's the processing going for {test_date.isoformat()}?",
    )

    fresh = await _run_agent_async(async_client, "why did it fail?", conversation_id=None)
    text = fresh["response"]
    print("\n=== SCENARIO 5 FRESH-CONVERSATION RESPONSE ===\n" + text + "\n=== END ===\n")

    # NOTE: the seeded segment code (e.g. "EQ") alone is too weak a signal
    # here — a fresh, context-free "why did it fail?" naturally falls back
    # to querying *real* today's status (get_edp_status defaults trade_date
    # to today when omitted), and real-today may legitimately have its own
    # EQ segment in some non-failed state, so the code alone would recur in
    # any truthful status answer regardless of leakage. The unambiguous
    # signal of real cross-conversation state leakage is the fixture's own
    # distinctive skip_reason text (seeded only on `test_date`'s row, in
    # `seeded_failed_day` above) or an explicit mention of `test_date`
    # itself showing up in a completely different conversation.
    leaked_reason = "simulated failure for live hallucination test" in text.lower()
    leaked_test_date = test_date.isoformat() in text
    names_segment_as_failed = failed_segment_code.lower() in text.lower() and "fail" in text.lower()

    if leaked_reason or leaked_test_date:
        print(
            "\n*** REPORTABLE FINDING: fresh/context-free conversation_id leaked "
            f"unambiguous state from a different conversation (leaked_reason="
            f"{leaked_reason}, leaked_test_date={leaked_test_date}) — real "
            "cross-conversation state leakage. ***\n"
        )
    elif names_segment_as_failed:
        print(
            "\n*** NOTE: fresh conversation mentioned segment "
            f"{failed_segment_code!r} alongside failure language, but with no "
            "unambiguous leaked marker (skip_reason/test_date) — likely a "
            "truthful answer about REAL today's status, not leakage from the "
            "seeded far-future test_date. Worth a manual look at the full "
            "response above. ***\n"
        )

    assert not (leaked_reason or leaked_test_date), (
        f"Fresh conversation (no conversation_id) leaked unambiguous state from "
        f"a different conversation's seeded day ({test_date.isoformat()}) — "
        f"instead of asking for clarification / admitting it lacks context. "
        f"Response:\n{text}"
    )
