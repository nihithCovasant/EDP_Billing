"""
The transition map built by TradeSegmentTransitionFactory isn't just
documentation — AbstractSegmentStateMachine.execute_handler() checks every
handler's proposed next_state against it (_validate_transition()) before
applying it. This test proves that safety net actually fires: a handler
that tries to jump straight from TRIGGERED to WAITING_FOR_CONTRACT_NOTE_
GENERATION (skipping WAITING_FOR_BILLPOSTING/WAITING_FOR_RECON — not a
registered edge for any real segment) must be caught and the segment
marked FAILED, not silently applied.

Without a test like this, a future refactor could quietly break
_validate_transition() (e.g. a wrong edge in TradeSegmentTransitionFactory)
and nothing would notice until a real handler bug slipped an illegal
transition into production.
"""

from __future__ import annotations

from src.agent.edp import repository
from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.state_machine import SegmentFactory
from src.agent.edp.state_machine.SegmentHandlerResult import ADVANCE, SegmentHandlerResult
from src.tools.cbos_client import CbosClient

from . import helpers

SEGMENT = "CUR"


async def test_illegal_state_skip_is_rejected_and_fails_segment(
    cfg, session_factory, test_date, monkeypatch,
):
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    fixed_now = helpers.fixed_now_for(test_date, orchestrator._tz)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = fixed_now
        row.current_state = SegmentState.TRIGGERED
        row.current_process = "BILLPOSTING"
        await session.commit()

    # TRIGGERED may only advance to WAITING_FOR_BILLPOSTING (or FAIL) —
    # force a handler bug that tries to skip straight to
    # WAITING_FOR_CONTRACT_NOTE_GENERATION.
    state_machine = SegmentFactory.get_segment_state_machine(SEGMENT)

    async def _buggy_handle_triggered(cbos, row, session, login_id, now):
        return SegmentHandlerResult(
            outcome=ADVANCE,
            next_state=SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION,
            next_process="CONTRACTNOTEGENERATION",
        )

    monkeypatch.setattr(state_machine, "handle_triggered", _buggy_handle_triggered)

    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = fixed_now
    outcome = await orchestrator._process_one_segment(SEGMENT)
    assert outcome == "failed"

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert row.segment_status == SegmentStatus.FAILED, (
        "an illegal transition must durably FAIL the row, not silently "
        "apply the bad state and keep looping"
    )
    assert row.skip_category == "SYSTEM_ERROR"
    assert "Invalid transition" in (row.skip_reason or "")
    assert "WAITING_FOR_CONTRACT_NOTE_GENERATION" in (row.skip_reason or "")
    # The row must NOT have been advanced to the illegal state.
    assert row.current_state != SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION


async def test_every_real_segment_and_post_trade_code_has_a_transition_map_entry():
    """Catches drift if SEGMENT_ORDER/POST_TRADE_ORDER ever grows without a
    matching entry being wired into TradeSegmentTransitionFactory."""
    from src.agent.edp.state_machine.TradeSegmentTransitionFactory import (
        POST_TRADE_TRANSITION_MAP,
        REAL_SEGMENT_TRANSITION_MAP,
    )
    from src.agent.edp.utils.constants import POST_TRADE_ORDER, SEGMENT_ORDER

    for code in SEGMENT_ORDER:
        transitions = REAL_SEGMENT_TRANSITION_MAP.get_segment_transitions(code)
        assert transitions, f"{code} has no declared transitions in REAL_SEGMENT_TRANSITION_MAP"
        assert SegmentStatus.FAILED in transitions[SegmentState.INIT]
        assert SegmentStatus.SKIPPED in transitions[SegmentState.INIT]
        assert SegmentStatus.COMPLETED in transitions[SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION]

    for code in POST_TRADE_ORDER:
        transitions = POST_TRADE_TRANSITION_MAP.get_segment_transitions(code)
        assert transitions, f"{code} has no declared transitions in POST_TRADE_TRANSITION_MAP"
        assert SegmentStatus.FAILED in transitions[SegmentState.WAITING_FOR_GTG]
        assert SegmentStatus.SKIPPED in transitions[SegmentState.WAITING_FOR_GTG], (
            "WAITING_FOR_GTG doubles as post-trade's holiday check (no separate INIT state)"
        )
        assert SegmentState.WAITING_FOR_COMPLETION in transitions[SegmentState.WAITING_FOR_GTG], (
            "the direct, already-triggered edge must be declared"
        )
        assert SegmentStatus.COMPLETED in transitions[SegmentState.WAITING_FOR_COMPLETION]
