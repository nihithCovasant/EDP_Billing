"""
Live, end-to-end agent scenario tests -- REAL LLM, REAL seeded database.

Unlike test_tool_routing.py (which mocks `get_llm_model` with a
`ScriptedLLM` to deterministically test tool-routing plumbing), these tests
let the REAL LangChain chat model run against the REAL `/agent/run`
endpoint, and assert on what it actually SAYS in response to natural
language status queries. The point is to catch real-world LLM behavior
(accurate reporting, robustness to phrasing, and -- critically -- whether
it hallucinates when there is no real data) rather than to test internal
routing/plumbing.

Data flow being exercised for real, end to end:

  test query -> POST /agent/run (via TestClient, in-process) -> real
  LangGraph ReAct loop -> real LLM decides to call get_edp_status -> that
  tool makes an httpx call to `/edp/status/{date}` -> that endpoint reads
  via `get_session()` (src/agent/edp/database.py) -> which the
  `wire_orchestrator_database` autouse fixture (tests/conftest.py) has
  pointed at THIS test's own engine, already seeded with
  `helpers.seed_day`/`helpers.drive_until_terminal` -> the LLM's final
  answer is asserted against the REAL, known data we seeded.

Plumbing note on `get_edp_status`'s self-call
----------------------------------------------
`get_edp_status` (src/tools/edp_status.py) builds a fresh
`httpx.AsyncClient()` and calls `http://localhost:{PORT}/edp/status/...` --
a real network call, not routed through Starlette's ASGI transport by
default. Two earlier approaches to make that self-call actually land on
this test's seeded data were tried and abandoned:

  1. A background-thread uvicorn server sharing this test's already-wired
     `engine` fixture -> intermittent
     `asyncpg.exceptions.InterfaceError: cannot perform operation: another
     operation is in progress`, because an asyncpg connection is bound to
     the event loop that created it, and a second thread's loop driving it
     through SQLAlchemy's greenlet bridge corrupts its internal state.
  2. A background-thread server with its OWN separate engine (its own
     connection pool, created inside that thread's own loop) ->
     `RuntimeError: ... attached to a different loop`.
  3. A genuinely separate OS subprocess (its own interpreter/loop/pool,
     reading the same DATABASE_URL) avoided the loop-sharing bug entirely,
     but is slow (real startup/migration cost per test) and fragile
     (health-check timing, log draining, port allocation).

A fourth approach -- `httpx.ASGITransport` patched into `get_edp_status`'s
`httpx.AsyncClient` construction, so the tool's self-call is dispatched
in-process into the SAME FastAPI `app` object the test's own `TestClient`
uses -- was also tried (see `_asgi_async_client_factory` /
`route_edp_status_through_app` below) and is the standard fix for "an app
that calls itself over HTTP" in tests. It did NOT fully resolve the issue
here: `TestClient` (and FastAPI's `BaseHTTPMiddleware`, which this app uses
via `OtelContextMiddleware`/`claims_middleware.py`) runs the ASGI app's
request handling inside its own anyio task group / portal, which is a
DIFFERENT asyncio loop context than the one `wire_orchestrator_database`
(tests/conftest.py) wired the test's `engine` fixture to. The nested
self-call from `get_edp_status` -> `ASGITransport` -> the app's own
`/edp/status/...` route -> `get_session()` therefore still crosses a loop
boundary, and intermittently fails with:

    RuntimeError: Task <Task pending name='starlette.middleware.base.
    BaseHTTPMiddleware.__call__.<locals>.call_next.<locals>.coro' ...>
    got Future <Future pending cb=[BaseProtocol._on_waiter_completed()]>
    attached to a different loop

(observed directly in a live run; see e.g. `test_segment_specific_query_is_
specific_to_that_segment`, which "passed" only because the LLM's resulting
error-handling response happened to satisfy that test's loose assertions --
not because it saw real seeded data). This is a genuine, documented
limitation of this test harness combined with `BaseHTTPMiddleware`'s
task-group semantics, not something resolved by trying yet another
transport/subprocess variant. The four DB-touching scenarios below (1, 2,
4, 5, and technically 3 as well -- see its own skip note) are marked
`@pytest.mark.skip` for that reason. Fixing it for real would need either
removing `BaseHTTPMiddleware` from the app's middleware stack for tests, or
restructuring `get_edp_status` to call the repository layer directly
instead of over HTTP in a test context -- both out of scope for a
test-only file that must not touch `src/`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_LIVE_AGENT_TESTS"),
    reason="Hits the real LLM (costs money, non-deterministic) -- set RUN_LIVE_AGENT_TESTS=1 to run.",
)

# Must be set before `src.agent.__main__` is imported, since `build_app()`
# reads this env var directly to decide whether to start the EDP wake loop
# (which otherwise runs Alembic migrations against a real Postgres DB).
os.environ["EDP_LOOP_ENABLED"] = "false"

from unittest.mock import patch

import httpx
import pytest_asyncio
from fastapi.testclient import TestClient

from src.agent.__main__ import build_app
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import SEGMENT_ORDER
import src.tools.edp_status as edp_status_module

from .. import helpers
from ..fakes import FailingCbosClient

FAILING_SEGMENT = "EQ"
FAILING_PROCESS = "BILLPOSTING"
TOTAL_SEGMENTS = len(SEGMENT_ORDER)
COMPLETED_SEGMENTS = TOTAL_SEGMENTS - 1


@pytest.fixture()
def app():
    return build_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def _asgi_async_client_factory(app):
    """
    Returns a drop-in replacement for `httpx.AsyncClient` that, regardless
    of how `get_edp_status`'s `_get`/`_post` helpers construct it (they only
    ever pass `timeout=`), always dispatches through `httpx.ASGITransport`
    bound to THIS test's `app` object -- so the tool's "self-call" to
    `/edp/status/...` runs in-process, on the same event loop as the test,
    against the same wired-up test database. No real socket is ever opened.
    """

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                timeout=30.0,
            )

    return _PatchedAsyncClient


@pytest.fixture()
def route_edp_status_through_app(app):
    """
    Patches `src.tools.edp_status.httpx.AsyncClient` (the exact name looked
    up inside `_get`/`_post`) for the duration of a test, so `get_edp_status`'s
    internal call is routed into `app` via ASGITransport instead of a real
    socket.
    """
    with patch.object(edp_status_module.httpx, "AsyncClient", _asgi_async_client_factory(app)):
        yield


def _ask(client: TestClient, query: str) -> str:
    """POST a real query to the real /agent/run endpoint (in-process via
    TestClient) and return the real response text."""
    resp = client.post("/agent/run", json={"query": query})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["response"]


@pytest_asyncio.fixture
async def seeded_day_with_one_failure(cfg, session_factory, test_date):
    """
    Seeds a real, known mix of outcomes for `test_date`: EQ fails at
    BILLPOSTING (a permanent CBOS error), every other segment completes
    normally. Drives the REAL orchestrator/state machine to a terminal
    state -- nothing about the pipeline logic itself is faked, only the
    CBOS response for this one (segment, process) pair.
    """
    cbos = FailingCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url,
        fail_segment=FAILING_SEGMENT, fail_process=FAILING_PROCESS,
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    return {r.segment_code: r for r in rows}


# ---------------------------------------------------------------------------
# 1. Day-level status query -> must report the correct total & call out the
#    real failed segment by code.
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "get_edp_status's self-call into the app crosses an event-loop boundary "
        "introduced by BaseHTTPMiddleware/TestClient's portal, intermittently raising "
        "'RuntimeError: ... got Future <Future pending cb=[BaseProtocol._on_waiter_completed()]> "
        "attached to a different loop' before the LLM ever sees real seeded status data."
    )
)
def test_day_status_query_reports_correct_counts_and_failed_segment(
    client, route_edp_status_through_app, seeded_day_with_one_failure, test_date,
):
    response_text = _ask(client, f"How is EDP processing going for {test_date.isoformat()}?")
    print(f"\n--- LIVE RESPONSE (day status) ---\n{response_text}\n")

    assert str(TOTAL_SEGMENTS) in response_text
    assert FAILING_SEGMENT in response_text
    assert any(
        word in response_text.lower() for word in ["fail", "issue", "problem", "error"]
    )


# ---------------------------------------------------------------------------
# 2. Same seeded day, differently phrased query -> must still surface the
#    real failed segment (robustness to real LLM phrasing variance).
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "Same asyncpg/event-loop-boundary limitation as test_day_status_query_"
        "reports_correct_counts_and_failed_segment: 'RuntimeError: ... got Future "
        "<Future pending cb=[BaseProtocol._on_waiter_completed()]> attached to a "
        "different loop' when get_edp_status calls back into the app."
    )
)
def test_differently_phrased_query_still_surfaces_failure(
    client, route_edp_status_through_app, seeded_day_with_one_failure, test_date,
):
    response_text = _ask(
        client,
        f"Any issues with the segments on {test_date.isoformat()}? What's failing right now?",
    )
    print(f"\n--- LIVE RESPONSE (rephrased) ---\n{response_text}\n")

    assert FAILING_SEGMENT in response_text
    assert any(
        word in response_text.lower() for word in ["fail", "issue", "problem", "error"]
    )


# ---------------------------------------------------------------------------
# 3. Segment-specific query -> must be specific to that ONE segment, not a
#    dump of the whole day's summary.
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "In a live run this test technically passed, but only because get_edp_status hit "
        "the same 'RuntimeError: ... attached to a different loop' event-loop-boundary error "
        "as the other scenarios, and the resulting generic error message happened to satisfy "
        "this test's loose assertions -- it never actually saw the real seeded EQ/BILLPOSTING "
        "failure data, so the pass was spurious, not a genuine verification."
    )
)
def test_segment_specific_query_is_specific_to_that_segment(
    client, route_edp_status_through_app, seeded_day_with_one_failure, test_date,
):
    other_segment = next(c for c in SEGMENT_ORDER if c != FAILING_SEGMENT)

    response_text = _ask(
        client, f"What's the status of segment {FAILING_SEGMENT} on {test_date.isoformat()}?"
    )
    print(f"\n--- LIVE RESPONSE (segment-specific) ---\n{response_text}\n")

    assert FAILING_SEGMENT in response_text
    assert any(
        word in response_text.lower() for word in ["fail", "billposting", "error"]
    )
    # Should not read like a full day dump listing every other segment code.
    assert other_segment not in response_text


# ---------------------------------------------------------------------------
# 4. A date with genuinely NO seeded data -> must not hallucinate status.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def empty_test_date(session_factory):
    """A second, independent far-future date with nothing seeded at all."""
    import uuid
    from datetime import date, timedelta

    offset_days = 60000 + (uuid.uuid4().int % 5000)
    trade_date = date.today() + timedelta(days=offset_days)
    await helpers.cleanup_day(session_factory, trade_date)
    yield trade_date
    await helpers.cleanup_day(session_factory, trade_date)


@pytest.mark.skip(
    reason=(
        "Same asyncpg/event-loop-boundary limitation: 'RuntimeError: ... got Future "
        "<Future pending cb=[BaseProtocol._on_waiter_completed()]> attached to a "
        "different loop' when get_edp_status calls back into the app -- this is the "
        "critical hallucination check, and it cannot be trusted to run until the "
        "loop-boundary issue is fixed (a false 'no data' response could mask either "
        "a real fix or a real hallucination)."
    )
)
def test_empty_date_does_not_hallucinate_status(
    client, route_edp_status_through_app, empty_test_date,
):
    response_text = _ask(client, f"How is EDP processing going for {empty_test_date.isoformat()}?")
    print(f"\n--- LIVE RESPONSE (empty date) ---\n{response_text}\n")

    lowered = response_text.lower()
    # Must indicate absence of data / not-yet-started, not invent numbers.
    assert any(
        phrase in lowered
        for phrase in [
            "no workflow", "no data", "not been processed", "hasn't", "has not",
            "not started", "no record", "no segments", "not yet",
        ]
    ), f"Expected an honest 'no data' answer, got: {response_text}"
    # Must NOT fabricate a completed/failed count for a day with zero rows.
    assert "completed" not in lowered or "0" in lowered or "no" in lowered


# ---------------------------------------------------------------------------
# 5. A segment code that doesn't exist -> must not invent a fake status.
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "Same asyncpg/event-loop-boundary limitation as the other scenarios: "
        "'RuntimeError: ... got Future <Future pending cb=[BaseProtocol._on_waiter_completed()]> "
        "attached to a different loop' when get_edp_status calls back into the app."
    )
)
def test_nonexistent_segment_code_is_handled_gracefully(
    client, route_edp_status_through_app, seeded_day_with_one_failure, test_date,
):
    response_text = _ask(
        client, f"What's the status of segment ZZZZZZ on {test_date.isoformat()}?"
    )
    print(f"\n--- LIVE RESPONSE (nonexistent segment) ---\n{response_text}\n")

    lowered = response_text.lower()
    assert any(
        phrase in lowered
        for phrase in [
            "don't recognize", "do not recognize", "not recognize", "no record",
            "not a valid", "not found", "unknown segment", "doesn't exist",
            "does not exist", "couldn't find", "could not find",
        ]
    ), f"Expected a graceful 'unrecognized segment' answer, got: {response_text}"
    # Must not claim a concrete status for a segment that was never seeded.
    for bogus_status in ["completed", "in progress", "pending"]:
        assert bogus_status not in lowered or "zzzzzz" not in lowered
