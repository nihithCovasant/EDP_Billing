"""
Pure, in-memory unit tests for SegmentTransitionMap and
TradeSegmentTransitionFactory — the authoritative transition-safety net
that AbstractSegmentStateMachine.execute_handler() checks every proposed
state change against before applying it (see
tests/test_invalid_transition_guard.py for the end-to-end DB-backed proof
that the guard actually fires). These tests need no database and no
async: they exercise the generic map data structure directly, and then
the real REAL_SEGMENT_TRANSITION_MAP / POST_TRADE_TRANSITION_MAP
singletons built at import time.

A bug in either the generic SegmentTransitionMap.is_allowed() special-casing
or in one of the two factory methods' declared edges would let an illegal
state jump (e.g. skipping straight past WAITING_FOR_BILLPOSTING/_RECON, or
a mid-pipeline SKIPPED) go completely undetected in production, since
nothing else in the codebase re-derives or re-checks these edges.
"""

from __future__ import annotations

from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.state_machine.SegmentTransitionMap import SegmentTransitionMap
from src.agent.edp.state_machine.TradeSegmentTransitionFactory import (
    POST_TRADE_TRANSITION_MAP,
    REAL_SEGMENT_TRANSITION_MAP,
    TradeSegmentTransitionFactory,
)
from src.agent.edp.utils.constants import POST_TRADE_ORDER, SEGMENT_ORDER

REAL_SEGMENT_HAPPY_PATH = (
    SegmentState.INIT,
    SegmentState.WAITING_FOR_FILE_UPLOAD,
    SegmentState.WAITING_FOR_INSTI_TRADE,  # V6 Step-10 gate
    SegmentState.TRIGGERED,
    SegmentState.WAITING_FOR_BILLPOSTING,
    SegmentState.WAITING_FOR_RECON,
    SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION,
    SegmentStatus.COMPLETED,
)

POST_TRADE_HAPPY_PATH = (
    SegmentState.WAITING_FOR_GTG,
    SegmentState.TRIGGERED,
    SegmentState.WAITING_FOR_COMPLETION,
    SegmentStatus.COMPLETED,
)


# --------------------------------------------------------------------------
# SegmentTransitionMap — generic data structure, exercised in isolation.
# --------------------------------------------------------------------------

def test_check_valid_segment_raises_for_unregistered_segment():
    m = SegmentTransitionMap(("EQ", "DR"))
    try:
        m.check_valid_segment("NOT_A_SEGMENT")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unregistered segment")


def test_check_valid_segment_does_not_raise_for_registered_segment():
    m = SegmentTransitionMap(("EQ", "DR"))
    m.check_valid_segment("EQ")  # must not raise


def test_same_state_is_always_allowed_even_on_an_empty_map():
    """The from_state == to_state no-op special case must short-circuit
    before any transition lookup — true even for a segment with zero
    registered edges."""
    m = SegmentTransitionMap(("EQ",))
    assert m.is_allowed("EQ", SegmentState.INIT, SegmentState.INIT) is True
    assert m.is_allowed(
        "EQ", SegmentState.WAITING_FOR_BILLPOSTING, SegmentState.WAITING_FOR_BILLPOSTING,
    ) is True


def test_unregistered_pair_is_rejected_on_a_map_with_some_transitions():
    """A map that DOES have some declared edges must still reject a pair
    that was never added — proves is_allowed doesn't just default to True
    once anything has been registered for that segment."""
    m = SegmentTransitionMap(("EQ",))
    m.add_allowed_transition("EQ", SegmentState.INIT, SegmentState.WAITING_FOR_FILE_UPLOAD)
    assert m.is_allowed("EQ", SegmentState.INIT, SegmentState.TRIGGERED) is False
    assert m.is_allowed(
        "EQ", SegmentState.WAITING_FOR_FILE_UPLOAD, SegmentState.INIT,
    ) is False


# --------------------------------------------------------------------------
# REAL_SEGMENT_TRANSITION_MAP
# --------------------------------------------------------------------------

def test_real_segment_happy_path_allowed_for_every_segment():
    for code in SEGMENT_ORDER:
        for from_state, to_state in zip(REAL_SEGMENT_HAPPY_PATH, REAL_SEGMENT_HAPPY_PATH[1:]):
            assert REAL_SEGMENT_TRANSITION_MAP.is_allowed(code, from_state, to_state), (
                f"{code}: {from_state} -> {to_state} should be allowed on the happy path"
            )


def test_real_segment_failed_reachable_from_every_non_terminal_state():
    segment = SEGMENT_ORDER[0]
    for state in REAL_SEGMENT_HAPPY_PATH[:-1]:  # exclude the terminal COMPLETED
        assert REAL_SEGMENT_TRANSITION_MAP.is_allowed(segment, state, SegmentStatus.FAILED), (
            f"FAILED should be reachable from {state}"
        )


def test_real_segment_skipped_reachable_only_from_init():
    segment = SEGMENT_ORDER[0]
    assert REAL_SEGMENT_TRANSITION_MAP.is_allowed(
        segment, SegmentState.INIT, SegmentStatus.SKIPPED,
    ) is True

    for state in REAL_SEGMENT_HAPPY_PATH[1:-1]:  # every state except INIT and terminal COMPLETED
        assert REAL_SEGMENT_TRANSITION_MAP.is_allowed(
            segment, state, SegmentStatus.SKIPPED,
        ) is False, (
            f"SKIPPED must NOT be reachable from {state} — a segment could "
            "otherwise be silently skipped mid-pipeline"
        )


def test_real_segment_skip_ahead_jump_is_rejected():
    """WAITING_FOR_FILE_UPLOAD -> WAITING_FOR_BILLPOSTING directly (skipping
    TRIGGERED) must never be allowed."""
    segment = SEGMENT_ORDER[0]
    assert REAL_SEGMENT_TRANSITION_MAP.is_allowed(
        segment, SegmentState.WAITING_FOR_FILE_UPLOAD, SegmentState.WAITING_FOR_BILLPOSTING,
    ) is False


# --------------------------------------------------------------------------
# POST_TRADE_TRANSITION_MAP
# --------------------------------------------------------------------------

def test_post_trade_happy_path_allowed_for_every_process():
    for code in POST_TRADE_ORDER:
        for from_state, to_state in zip(POST_TRADE_HAPPY_PATH, POST_TRADE_HAPPY_PATH[1:]):
            assert POST_TRADE_TRANSITION_MAP.is_allowed(code, from_state, to_state), (
                f"{code}: {from_state} -> {to_state} should be allowed on the happy path"
            )


def test_post_trade_failed_reachable_from_every_non_terminal_state():
    process = POST_TRADE_ORDER[0]
    for state in POST_TRADE_HAPPY_PATH[:-1]:  # exclude the terminal COMPLETED
        assert POST_TRADE_TRANSITION_MAP.is_allowed(process, state, SegmentStatus.FAILED), (
            f"FAILED should be reachable from {state}"
        )


def test_post_trade_skipped_reachable_only_from_waiting_for_gtg():
    process = POST_TRADE_ORDER[0]
    assert POST_TRADE_TRANSITION_MAP.is_allowed(
        process, SegmentState.WAITING_FOR_GTG, SegmentStatus.SKIPPED,
    ) is True

    for state in POST_TRADE_HAPPY_PATH[1:-1]:  # every state except WAITING_FOR_GTG and terminal COMPLETED
        assert POST_TRADE_TRANSITION_MAP.is_allowed(
            process, state, SegmentStatus.SKIPPED,
        ) is False, (
            f"SKIPPED must NOT be reachable from {state} — post-trade's "
            "holiday check only happens on the first WAITING_FOR_GTG poll"
        )


def test_post_trade_direct_already_triggered_shortcut_is_allowed():
    """WAITING_FOR_GTG -> WAITING_FOR_COMPLETION directly (skipping
    TRIGGERED) is the documented "already triggered" shortcut and must be
    allowed, not just the via-TRIGGERED path."""
    for code in POST_TRADE_ORDER:
        assert POST_TRADE_TRANSITION_MAP.is_allowed(
            code, SegmentState.WAITING_FOR_GTG, SegmentState.WAITING_FOR_COMPLETION,
        ) is True


# --------------------------------------------------------------------------
# Drift guard: every declared code must actually have entries wired in.
# --------------------------------------------------------------------------

def test_every_real_segment_code_has_a_non_empty_transitions_entry():
    for code in SEGMENT_ORDER:
        transitions = REAL_SEGMENT_TRANSITION_MAP.get_segment_transitions(code)
        assert transitions, f"{code} has no declared transitions in REAL_SEGMENT_TRANSITION_MAP"


def test_every_post_trade_code_has_a_non_empty_transitions_entry():
    for code in POST_TRADE_ORDER:
        transitions = POST_TRADE_TRANSITION_MAP.get_segment_transitions(code)
        assert transitions, f"{code} has no declared transitions in POST_TRADE_TRANSITION_MAP"


def test_factory_methods_are_directly_rebuildable_and_match_singletons():
    """Sanity check that the module-level singletons really are just the
    factory methods applied to SEGMENT_ORDER/POST_TRADE_ORDER, so testing
    the singletons above is equivalent to testing the factory directly."""
    rebuilt_real = TradeSegmentTransitionFactory.load_segment_transition_map(SEGMENT_ORDER)
    rebuilt_post_trade = TradeSegmentTransitionFactory.load_post_trade_transition_map(POST_TRADE_ORDER)

    for code in SEGMENT_ORDER:
        assert (
            rebuilt_real.get_segment_transitions(code)
            == REAL_SEGMENT_TRANSITION_MAP.get_segment_transitions(code)
        )
    for code in POST_TRADE_ORDER:
        assert (
            rebuilt_post_trade.get_segment_transitions(code)
            == POST_TRADE_TRANSITION_MAP.get_segment_transitions(code)
        )
