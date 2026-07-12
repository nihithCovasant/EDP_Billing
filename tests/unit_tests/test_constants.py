"""
Unit tests for the fixed segment/post-trade-process constants and their
lookup helpers in src/agent/edp/utils/constants.py.

- SEGMENT_ORDER/POST_TRADE_ORDER are the regulatory processing sequence;
  SEGMENT_NAMES/POST_TRADE_NAMES/POST_TRADE_GTG_PROCESS_NAME are display/
  API-name lookups keyed off those same codes. A code present in an
  *_ORDER tuple but missing from its corresponding name dict would
  silently leak the raw code into the UI/API instead of a human label —
  the completeness tests below guard against that class of bug.
- get_sequence_order()/is_post_trade_process() together implement the
  "9 real segments sort before 5 post-trade processes, unknown codes sort
  last without raising" contract documented on get_sequence_order(); the
  no-overlap test guards the invariant that classification depends on,
  namely that a code can't be in both ORDER tuples at once.
"""

from __future__ import annotations

from datetime import timedelta

from src.agent.edp.utils.constants import (
    NEXT_DAY_WINDOW_SEGMENTS,
    POST_TRADE_GTG_PROCESS_NAME,
    POST_TRADE_NAMES,
    POST_TRADE_ORDER,
    SEGMENT_NAMES,
    SEGMENT_ORDER,
    STALE_HEARTBEAT_THRESHOLD,
    get_segment_name,
    get_sequence_order,
    is_post_trade_process,
)


def test_every_real_segment_has_a_display_name():
    """A segment missing from SEGMENT_NAMES would show its raw code
    (e.g. "NCDEXPHY") in the API instead of a human label."""
    for code in SEGMENT_ORDER:
        assert code in SEGMENT_NAMES


def test_every_post_trade_process_has_a_display_name():
    for code in POST_TRADE_ORDER:
        assert code in POST_TRADE_NAMES


def test_every_post_trade_process_has_a_gtg_process_name():
    """Missing here would break the GTG poll's ProcessName field for that
    process, since POST_TRADE_GTG_PROCESS_NAME is the only source for it."""
    for code in POST_TRADE_ORDER:
        assert code in POST_TRADE_GTG_PROCESS_NAME


def test_sequence_order_first_and_last_real_segments():
    assert get_sequence_order(SEGMENT_ORDER[0]) == 1
    assert get_sequence_order(SEGMENT_ORDER[-1]) == len(SEGMENT_ORDER)


def test_sequence_order_matches_declared_segment_order():
    for i, code in enumerate(SEGMENT_ORDER, start=1):
        assert get_sequence_order(code) == i


def test_sequence_order_post_trade_continues_after_real_segments():
    base = len(SEGMENT_ORDER)
    for i, code in enumerate(POST_TRADE_ORDER, start=1):
        assert get_sequence_order(code) == base + i


def test_sequence_order_unrecognized_code_sorts_last_without_raising():
    """Deliberate per the docstring: an unexpected segment_code must sort
    last (999), not raise, so it can't crash the day's ordering."""
    assert get_sequence_order("NOT_A_REAL_CODE") == 999


def test_segment_name_known_real_segment():
    assert get_segment_name("EQ") == SEGMENT_NAMES["EQ"]


def test_segment_name_known_post_trade_process():
    assert get_segment_name("COLVAL") == POST_TRADE_NAMES["COLVAL"]


def test_segment_name_unrecognized_code_falls_back_to_code_itself():
    """get_segment_name() checks SEGMENT_NAMES then falls back to
    POST_TRADE_NAMES.get(code, code) — an unknown code returns unchanged."""
    assert get_segment_name("MADE_UP_CODE") == "MADE_UP_CODE"


def test_is_post_trade_process_true_for_every_post_trade_code():
    for code in POST_TRADE_ORDER:
        assert is_post_trade_process(code) is True


def test_is_post_trade_process_false_for_every_real_segment():
    for code in SEGMENT_ORDER:
        assert is_post_trade_process(code) is False


def test_is_post_trade_process_false_for_nonsense_string():
    assert is_post_trade_process("NOT_A_REAL_CODE") is False


def test_no_overlap_between_segment_order_and_post_trade_order():
    """A code accidentally present in both would break
    is_post_trade_process()'s binary classification."""
    assert set(SEGMENT_ORDER).isdisjoint(set(POST_TRADE_ORDER))


def test_next_day_window_segments_are_the_documented_mcx_nsecom_trio():
    assert NEXT_DAY_WINDOW_SEGMENTS == frozenset({"MCX", "MCXPHY", "NSECOM"})


def test_next_day_window_segments_are_a_subset_of_real_segments():
    assert NEXT_DAY_WINDOW_SEGMENTS.issubset(set(SEGMENT_ORDER))


def test_stale_heartbeat_threshold_is_a_positive_timedelta():
    assert isinstance(STALE_HEARTBEAT_THRESHOLD, timedelta)
    assert STALE_HEARTBEAT_THRESHOLD > timedelta(0)
