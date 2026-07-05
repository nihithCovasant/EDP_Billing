"""
An unmapped current_phase must mark the row FAILED, not silently retry it
forever.

pipeline.executor.advance_pipeline() looks up the right handler for
row.current_phase in whichever phase_handlers dict it's given (the 7-step
real-segment dict or the 3-step post-trade dict). If a future migration
ever adds a new SegmentPhase enum value without updating both handler
dicts, or a segment's phase and pipeline get mismatched some other way,
the lookup fails. Before this test's fix, that just logged an error and
returned the string "failed" WITHOUT ever touching row.segment_status —
the row stayed IN_PROGRESS at the same unmapped phase, picked up again
every cycle, hitting the same wall silently forever (visible only in logs,
never in the DB or any status API).

Reproduced here using a REAL SegentPhase value that genuinely isn't wired
for the pipeline being driven: AWAIT_GTG only exists in
_POST_TRADE_PHASE_HANDLERS, not the default 7-step _PHASE_HANDLERS used
for real segments — so driving a real segment ("CUR") through the default
pipeline while it's sitting at AWAIT_GTG is a faithful, code-real instance
of "no handler registered for this phase", no fake enum value needed.
"""

from __future__ import annotations

from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient

from . import helpers

SEGMENT = "CUR"


async def test_unmapped_phase_marks_segment_failed(cfg, session_factory, test_date):
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    fixed_now = helpers.fixed_now_for(test_date, orchestrator._tz)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = fixed_now
        row.current_phase = SegmentPhase.AWAIT_GTG  # not in the 7-step handler dict
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
    assert "AWAIT_GTG" in (row.skip_reason or "")
    assert row.completed_at is not None

    # Halt-on-FAILED semantics apply here too — nothing after CUR should run.
    rows = await helpers.get_rows(session_factory, test_date)
    idx = [r.segment_code for r in rows].index(SEGMENT)
    for row in rows[idx + 1:]:
        assert row.segment_status == SegmentStatus.PENDING
