"""
Live, end-to-end agent scenario tests — REAL LLM, REAL seeded database.

Unlike test_tool_routing.py (which mocks `get_llm_model` with a
`ScriptedLLM` to deterministically test tool-routing plumbing), these tests
let the REAL LangChain chat model run against the REAL `/agent/run`
endpoint, and assert on what it actually SAYS in response to natural
language status queries. The point is to catch real-world LLM behavior
(accurate reporting, robustness to phrasing, and — critically — whether it
hallucinates when there is no real data) rather than to test internal
routing/plumbing.

Data flow being exercised for real, end to end:

  test query -> POST /agent/run -> real LangGraph ReAct loop -> real LLM
  decides to call get_edp_status -> that tool makes a REAL httpx call to
  this same process's own `/edp/status/{date}` endpoint -> that endpoint
  reads via `get_session()` (src/agent/edp/database.py) -> which the
  `wire_orchestrator_database` autouse fixture (tests/conftest.py) has
  pointed at the SAME test engine we seeded with `helpers.seed_day` /
  `helpers.drive_until_terminal` -> the LLM's final answer is asserted
  against the REAL, known data we seeded.

Because `get_edp_status` (src/tools/edp_status.py) calls
`http://localhost:{PORT}` with a real `httpx.AsyncClient` — not through
Starlette's ASGI transport — a `fastapi.testclient.TestClient` alone is not
enough (it never binds a real socket). So this file additionally boots a
REAL uvicorn server, in a background thread, bound to the `PORT` from
.env/settings, sharing this test process's (and hence this test's wired-up
test engine's) memory space.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_LIVE_AGENT_TESTS"),
    reason="Hits the real LLM (costs money, non-deterministic) — set RUN_LIVE_AGENT_TESTS=1 to run.",
)

# Must be set before `src.agent.__main__` is imported — build_app() reads
# this directly to decide whether to start the EDP wake loop (which would
# otherwise run Alembic migrations against a real Postgres DB on import).
os.environ["EDP_LOOP_ENABLED"] = "false"

import socket

import httpx
import uvicorn
import pytest_asyncio

from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import SEGMENT_ORDER

from .. import helpers
from ..fakes import FailingCbosClient

FAILING_SEGMENT = "EQ"
FAILING_PROCESS = "BILLPOSTING"
TOTAL_SEGMENTS = len(SEGMENT_ORDER)
COMPLETED_SEGMENTS = TOTAL_SEGMENTS - 1


def _free_port() -> int:
    """A free local port — NOT the dev-server's PORT from .env, since a real
    agent instance may already be listening there. `get_edp_status`'s
    internal httpx call reads os.getenv("PORT") at call time (see
    src/tools/edp_status.py::_base_url()), so we set PORT to this chosen
    free port before building/starting the app, and the tool call will
    follow it automatically."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LiveServer:
    """
    Runs the real FastAPI app on a real socket, in a background thread, for
    the lifetime of one test — so `get_edp_status`'s internal `httpx` call
    to `http://localhost:{PORT}/edp/status/...` hits a real, listening
    server backed by the same test database as the test itself.

    Deliberately does NOT create its own engine on the server thread's own
    event loop (an earlier attempt at that raised
    "RuntimeError: ... attached to a different loop" /
    "asyncpg.exceptions.InterfaceError: cannot perform operation: another
    operation is in progress" — asyncpg pools are loop-bound, and a second
    pool touching the same DB from a second loop while the test's own loop
    is mid-transaction races the first). Instead this reuses the SAME
    already-initialized `edp_database._engine`/`_session_factory` globals
    that the `wire_orchestrator_database` autouse fixture (tests/conftest.py)
    already pointed at this test's own `engine` fixture — so there is only
    ever ONE engine/pool in play, just invoked from two different threads
    sequentially (never concurrently, since each HTTP call is awaited to
    completion before the next one is made).
    """

    def __init__(self, port: int):
        self.port = port
        os.environ["PORT"] = str(port)
        # Imported here (not at module top) so it's built AFTER PORT is set
        # and AFTER EDP_LOOP_ENABLED=false is set above.
        from src.agent.__main__ import build_app

        app = build_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.1)
        raise RuntimeError("Live uvicorn server did not start in time")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def live_server(engine):
    """
    Function-scoped (not module-scoped) and depends on `engine` so it is
    created AFTER the `wire_orchestrator_database` autouse fixture has
    already pointed `edp_database._engine`/`_session_factory` at THIS test's
    own engine — the server thread then reuses those same globals rather
    than racing to create a second, loop-bound pool.
    """
    port = _free_port()
    srv = _LiveServer(port)
    srv.start()
    yield srv
    srv.stop()


def _ask(query: str, live_server: "_LiveServer") -> str:
    """POST a real query to the real /agent/run endpoint and return the
    real response text."""
    resp = httpx.post(
        f"http://127.0.0.1:{live_server.port}/agent/run",
        json={"query": query},
        timeout=120.0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["response"]


@pytest_asyncio.fixture
async def seeded_day_with_one_failure(cfg, session_factory, test_date, live_server):
    """
    Seeds a real, known mix of outcomes for `test_date`: EQ fails at
    BILLPOSTING (a permanent CBOS error), every other segment completes
    normally. Drives the REAL orchestrator/state machine to a terminal
    state — nothing about the pipeline logic itself is faked, only the
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

def test_day_status_query_reports_correct_counts_and_failed_segment(
    seeded_day_with_one_failure, test_date, live_server,
):
    response_text = _ask(
        f"How is EDP processing going for {test_date.isoformat()}?", live_server,
    )
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

def test_differently_phrased_query_still_surfaces_failure(
    seeded_day_with_one_failure, test_date, live_server,
):
    response_text = _ask(
        f"Any issues with the segments on {test_date.isoformat()}? What's failing right now?",
        live_server,
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

def test_segment_specific_query_is_specific_to_that_segment(
    seeded_day_with_one_failure, test_date, live_server,
):
    other_segment = next(c for c in SEGMENT_ORDER if c != FAILING_SEGMENT)

    response_text = _ask(
        f"What's the status of segment {FAILING_SEGMENT} on {test_date.isoformat()}?",
        live_server,
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


def test_empty_date_does_not_hallucinate_status(empty_test_date, live_server):
    response_text = _ask(
        f"How is EDP processing going for {empty_test_date.isoformat()}?", live_server,
    )
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

def test_nonexistent_segment_code_is_handled_gracefully(
    seeded_day_with_one_failure, test_date, live_server,
):
    response_text = _ask(
        f"What's the status of segment ZZZZZZ on {test_date.isoformat()}?",
        live_server,
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
