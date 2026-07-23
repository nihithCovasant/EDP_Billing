"""
An unmapped current_state must mark the row FAILED, not silently retry it
forever.

Reproduced with a real SegmentState value that genuinely isn't wired for
the pipeline being driven: WAITING_FOR_GTG only exists in
PostTradeStateMachine's handler dict, not RealSegmentStateMachine's — so
driving segment "CUR" while it's sitting at WAITING_FOR_GTG is a faithful
instance of "no handler registered for this state".
"""

from __future__ import annotations

from src.agent.edp import repository
from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient

from . import helpers

SEGMENT = "CUR"


async def test_unmapped_state_marks_segment_failed(cfg, session_factory, test_date):
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    fixed_now = helpers.fixed_now_for(test_date, orchestrator._tz)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = fixed_now
        row.current_state = SegmentState.WAITING_FOR_GTG  # not in the real-segment handler dict
        row.current_process = None
        await session.commit()

    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = fixed_now
    outcome = await orchestrator._process_one_segment(SEGMENT)
    assert outcome == "failed"

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert row.segment_status == SegmentStatus.FAILED, (
        "must be durably FAILED in the DB, not just logged — otherwise the "
        "row stays IN_PROGRESS and gets silently retried every cycle forever"
    )
    assert row.skip_category == "SYSTEM_ERROR"
    assert "WAITING_FOR_GTG" in (row.skip_reason or "")
    assert row.completed_at is not None

    # Only CUR was driven directly in this test — every other segment is
    # untouched and still PENDING.
    rows = await helpers.get_rows(session_factory, test_date)
    idx = [r.segment_code for r in rows].index(SEGMENT)
    for row in rows[idx + 1 :]:
        assert row.segment_status == SegmentStatus.PENDING
