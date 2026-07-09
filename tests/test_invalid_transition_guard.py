"""
The transition map built by TradeSegmentTransitionFactory isn't just
documentation — AbstractSegmentStateMachine.execute_handler() checks every
handler's proposed next_phase against it (_validate_transition()) before
applying it. This test proves that safety net actually fires: a handler
that tries to jump straight from TRIGGER to AWAIT_CONTRACT_NOTE (skipping
AWAIT_BILLPOSTING/AWAIT_RECON — not a registered edge for any real segment)
must be caught and the segment marked FAILED, not silently applied.

Without a test like this, a future refactor could quietly break
_validate_transition() (e.g. an off-by-one in the phase chain) and nothing
would notice until a real handler bug slipped an illegal transition into
production.
"""

from __future__ import annotations

from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.state_machine import SegmentFactory
from src.agent.edp.state_machine.SegmentHandlerResult import STOP_NEXT, SegmentHandlerResult
from src.tools.cbos_client import CbosClient

from . import helpers

SEGMENT = "CUR"


async def test_illegal_phase_skip_is_rejected_and_fails_segment(
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
        row.current_phase = SegmentPhase.TRIGGER
        row.current_process = "BILLPOSTING"
        await session.commit()

    # TRIGGER may only advance to AWAIT_BILLPOSTING (or FAIL/SKIP) — force a
    # handler bug that tries to skip straight to AWAIT_CONTRACT_NOTE.
    state_machine = SegmentFactory.get_segment_state_machine(SEGMENT)

    async def _buggy_handle_trigger(cbos, row, session, login_id, now):
        return SegmentHandlerResult(
            outcome=STOP_NEXT,
            next_phase=SegmentPhase.AWAIT_CONTRACT_NOTE,
            next_process="CONTRACTNOTEGENERATION",
        )

    monkeypatch.setattr(state_machine, "handle_trigger", _buggy_handle_trigger)

    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = fixed_now
    outcome = await orchestrator._process_one_segment(SEGMENT)
    assert outcome == "failed"

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert row.segment_status == SegmentStatus.FAILED, (
        "an illegal transition must durably FAIL the row, not silently "
        "apply the bad phase and keep looping"
    )
    assert row.skip_category == "SYSTEM_ERROR"
    assert "Invalid transition" in (row.skip_reason or "")
    assert "AWAIT_CONTRACT_NOTE" in (row.skip_reason or "")
    # The row must NOT have been advanced to the illegal phase.
    assert row.current_phase != SegmentPhase.AWAIT_CONTRACT_NOTE


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
        assert SegmentStatus.FAILED in transitions[SegmentPhase.HOLIDAY_CHECK]
        assert SegmentStatus.COMPLETED in transitions[SegmentPhase.AWAIT_CONTRACT_NOTE]

    for code in POST_TRADE_ORDER:
        transitions = POST_TRADE_TRANSITION_MAP.get_segment_transitions(code)
        assert transitions, f"{code} has no declared transitions in POST_TRADE_TRANSITION_MAP"
        assert SegmentStatus.FAILED in transitions[SegmentPhase.AWAIT_GTG]
        assert SegmentStatus.COMPLETED in transitions[SegmentPhase.AWAIT_CONFIRM]
