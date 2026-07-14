"""
Equivalence partitioning tests for segment_code handling across:

- constants.get_sequence_order / get_segment_name / is_post_trade_process
- SegmentTransitionMap.check_valid_segment / is_allowed
  (via the real REAL_SEGMENT_TRANSITION_MAP / POST_TRADE_TRANSITION_MAP
  instances built by TradeSegmentTransitionFactory)
- SegmentFactory.get_segment_state_machine

segment_code is typed as `str` everywhere but nothing enforces that at
runtime, and several call sites (config uploads, upstream API responses)
could plausibly hand these functions something other than a clean,
uppercase, exact SEGMENT_ORDER/POST_TRADE_ORDER member. These tests
partition segment_code into valid/invalid equivalence classes (plus the
documented boundary members of SEGMENT_ORDER/POST_TRADE_ORDER) and assert
on ACTUAL observed behavior, flagging anywhere behavior is inconsistent
across the four functions under test.
"""

from __future__ import annotations

import pytest

from src.agent.edp.utils.constants import (
    POST_TRADE_ORDER,
    SEGMENT_ORDER,
    get_segment_name,
    get_sequence_order,
    is_post_trade_process,
)
from src.agent.edp.state_machine.SegmentFactory import SegmentFactory
from src.agent.edp.state_machine.TradeSegmentTransitionFactory import (
    POST_TRADE_TRANSITION_MAP,
    REAL_SEGMENT_TRANSITION_MAP,
)


# ---------------------------------------------------------------------------
# Class 1: Valid real segment — boundaries (first/last of SEGMENT_ORDER) plus
# one from the middle.
# ---------------------------------------------------------------------------


def test_first_real_segment_is_order_1():
    """Boundary: first element of SEGMENT_ORDER ("EQ") must resolve to 1."""
    assert SEGMENT_ORDER[0] == "EQ"
    assert get_sequence_order("EQ") == 1
    assert get_segment_name("EQ") == "Cash"
    assert is_post_trade_process("EQ") is False


def test_last_real_segment_is_order_len_segment_order():
    """Boundary: last element of SEGMENT_ORDER ("NSECOM") must resolve to
    len(SEGMENT_ORDER), i.e. 9."""
    assert SEGMENT_ORDER[-1] == "NSECOM"
    assert get_sequence_order("NSECOM") == len(SEGMENT_ORDER) == 9
    assert get_segment_name("NSECOM") == "NSE Commodity"
    assert is_post_trade_process("NSECOM") is False


def test_middle_real_segment_ncdex():
    """Representative from the middle of SEGMENT_ORDER."""
    assert get_sequence_order("NCDEX") == SEGMENT_ORDER.index("NCDEX") + 1
    assert get_segment_name("NCDEX") == "NCDEX"
    assert is_post_trade_process("NCDEX") is False


def test_real_segment_recognized_by_transition_map_and_factory():
    """A valid real segment must be accepted by both the transition map and
    the state-machine factory (cross-checking the other two target modules
    against the same equivalence class)."""
    REAL_SEGMENT_TRANSITION_MAP.check_valid_segment("EQ")  # must not raise
    machine = SegmentFactory.get_segment_state_machine("EQ")
    assert machine.__class__.__name__ == "RealSegmentStateMachine"
    assert machine.SEGMENT_CODE == "EQ"


# ---------------------------------------------------------------------------
# Class 2: Valid post-trade code — boundaries (first/last of POST_TRADE_ORDER).
# ---------------------------------------------------------------------------


def test_first_post_trade_code_is_order_len_segment_order_plus_1():
    """Boundary: first element of POST_TRADE_ORDER ("COLVAL") sorts
    immediately after all 9 real segments, i.e. order 10."""
    assert POST_TRADE_ORDER[0] == "COLVAL"
    assert get_sequence_order("COLVAL") == len(SEGMENT_ORDER) + 1 == 10
    assert get_segment_name("COLVAL") == "Collateral Valuation"
    assert is_post_trade_process("COLVAL") is True


def test_last_post_trade_code_is_final_order_slot():
    """Boundary: last element of POST_TRADE_ORDER ("DMSTMT") sorts last,
    i.e. len(SEGMENT_ORDER) + len(POST_TRADE_ORDER) == 14."""
    assert POST_TRADE_ORDER[-1] == "DMSTMT"
    expected = len(SEGMENT_ORDER) + len(POST_TRADE_ORDER)
    assert get_sequence_order("DMSTMT") == expected == 14
    assert get_segment_name("DMSTMT") == "Daily Margin Statements"
    assert is_post_trade_process("DMSTMT") is True


def test_post_trade_code_recognized_by_transition_map_and_factory():
    POST_TRADE_TRANSITION_MAP.check_valid_segment("COLVAL")  # must not raise
    machine = SegmentFactory.get_segment_state_machine("COLVAL")
    assert machine.__class__.__name__ == "PostTradeStateMachine"
    assert machine.SEGMENT_CODE == "COLVAL"
    assert machine.TRIGGER_METHOD_NAME == "trigger_collateral_valuation"


# ---------------------------------------------------------------------------
# Class 3: Case variation of a valid code ("eq" / "Eq" for "EQ").
#
# No function normalizes case (all are exact dict/tuple membership checks),
# so a lowercase/mixed-case code is consistently unrecognized everywhere —
# no crash, but a latent risk if an upstream source ever supplies "eq".
# ---------------------------------------------------------------------------


def test_lowercase_valid_code_is_treated_as_unrecognized_everywhere():
    assert get_sequence_order("eq") == 999
    assert get_segment_name("eq") == "eq"  # falls through both maps, echoes input
    assert is_post_trade_process("eq") is False

    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment("eq")

    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine("eq")


def test_mixed_case_valid_code_is_treated_as_unrecognized_everywhere():
    """Same as above with mixed case ("Eq") — confirms it's a straight
    case-sensitive equality check, not e.g. a first-letter-only check."""
    assert get_sequence_order("Eq") == 999
    assert get_segment_name("Eq") == "Eq"
    assert is_post_trade_process("Eq") is False

    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment("Eq")

    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine("Eq")


# ---------------------------------------------------------------------------
# Class 3b: Whitespace-padded valid code (" EQ" / "EQ ").
#
# No function trims whitespace either — same "consistently unrecognized,
# nowhere normalized" shape as the case-variation class. Realistic failure
# mode: a config upload with a trailing space/newline from a spreadsheet
# cell would silently fall through to "unrecognized" rather than being
# rejected loudly or matched leniently.
# ---------------------------------------------------------------------------


def test_leading_whitespace_valid_code_is_not_trimmed():
    assert get_sequence_order(" EQ") == 999
    assert get_segment_name(" EQ") == " EQ"
    assert is_post_trade_process(" EQ") is False
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment(" EQ")
    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine(" EQ")


def test_trailing_whitespace_valid_code_is_not_trimmed():
    assert get_sequence_order("EQ ") == 999
    assert get_segment_name("EQ ") == "EQ "
    assert is_post_trade_process("EQ ") is False
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment("EQ ")
    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine("EQ ")


# ---------------------------------------------------------------------------
# Class 4: Empty string.
# ---------------------------------------------------------------------------


def test_empty_string_is_unrecognized_not_a_crash():
    assert get_sequence_order("") == 999
    assert get_segment_name("") == ""  # echoes back the (empty) input
    assert is_post_trade_process("") is False
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment("")
    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine("")


# ---------------------------------------------------------------------------
# Class 5: None (not a string at all — type hints aren't enforced at
# runtime). All four functions handle it gracefully (no TypeError anywhere),
# somewhat surprising given the `str` type hints. get_segment_name(None) is
# the one exception worth calling out: it has an explicit None guard
# returning "UNKNOWN" so it honors its `-> str` annotation instead of
# silently returning None.
# ---------------------------------------------------------------------------


def test_none_sequence_order_falls_back_to_999_no_crash():
    assert get_sequence_order(None) == 999  # type: ignore[arg-type]


def test_none_segment_name_returns_unknown_string_not_none():
    """get_segment_name() has an explicit None guard returning "UNKNOWN",
    honoring its `-> str` annotation instead of silently returning None."""
    result = get_segment_name(None)  # type: ignore[arg-type]
    assert result == "UNKNOWN"
    assert isinstance(result, str)


def test_none_is_post_trade_process_returns_false_no_crash():
    assert is_post_trade_process(None) is False  # type: ignore[arg-type]


def test_none_check_valid_segment_raises_value_error_not_type_error():
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment(None)  # type: ignore[arg-type]


def test_none_segment_factory_raises_value_error_not_type_error():
    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Class 6: A code valid in one family's list queried against the OTHER
# family's transition map -- must raise ValueError per check_valid_segment's
# own contract.
# ---------------------------------------------------------------------------


def test_post_trade_code_rejected_by_real_segment_transition_map():
    """"COLVAL" is a valid post-trade code but is NOT in SEGMENT_ORDER, so
    REAL_SEGMENT_TRANSITION_MAP (built over SEGMENT_ORDER) must reject it."""
    with pytest.raises(ValueError, match="COLVAL"):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment("COLVAL")


def test_real_segment_code_rejected_by_post_trade_transition_map():
    """Symmetric case: "EQ" is a valid real segment but not a post-trade
    code, so POST_TRADE_TRANSITION_MAP must reject it."""
    with pytest.raises(ValueError, match="EQ"):
        POST_TRADE_TRANSITION_MAP.check_valid_segment("EQ")


def test_is_allowed_also_enforces_family_boundary_via_check_valid_segment():
    """is_allowed() delegates to check_valid_segment() for any transition
    that isn't a same-state no-op, so it must raise too rather than
    silently returning False."""
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.is_allowed("COLVAL", "INIT", "TRIGGERED")


def test_is_allowed_same_state_noop_skips_validation_even_for_foreign_code():
    """Documented exception: from_state == to_state short-circuits to True
    *before* check_valid_segment runs, even for a segment_code from the
    wrong family. This is intentional per the docstring ("BLOCKED --
    nothing changed"), not a validation bypass in practice since no real
    caller drives a no-op transition off a foreign code, but it does mean
    check_valid_segment isn't unconditionally enforced by is_allowed."""
    assert REAL_SEGMENT_TRANSITION_MAP.is_allowed("COLVAL", "INIT", "INIT") is True


# ---------------------------------------------------------------------------
# Class 7: Plausible near-miss typos -- confirm no substring/fuzzy matching
# anywhere (tuple/dict membership is exact-match only).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typo_code", ["EQU", "E Q", "NCDEX2", "NCDE", "EQQ"])
def test_near_miss_typos_are_unrecognized_not_fuzzy_matched(typo_code):
    assert get_sequence_order(typo_code) == 999
    assert get_segment_name(typo_code) == typo_code  # echoed back, not matched
    assert is_post_trade_process(typo_code) is False
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment(typo_code)
    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine(typo_code)


# ---------------------------------------------------------------------------
# Class 8: Very long string -- defensive check that nothing chokes/hangs.
# ---------------------------------------------------------------------------


def test_very_long_string_does_not_choke_or_hang():
    long_code = "X" * 10_000
    assert get_sequence_order(long_code) == 999
    assert get_segment_name(long_code) == long_code
    assert is_post_trade_process(long_code) is False
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment(long_code)
    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine(long_code)


# ---------------------------------------------------------------------------
# Class 9: Unicode / special characters (embedded null byte, trademark
# symbol) -- confirm graceful handling, not a crash.
# ---------------------------------------------------------------------------


def test_embedded_null_byte_is_unrecognized_not_a_crash():
    code = "EQ\x00"
    assert get_sequence_order(code) == 999
    assert get_segment_name(code) == code
    assert is_post_trade_process(code) is False
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment(code)
    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine(code)


def test_unicode_trademark_symbol_is_unrecognized_not_a_crash():
    code = "EQ™"  # "EQ™"
    assert get_sequence_order(code) == 999
    assert get_segment_name(code) == code
    assert is_post_trade_process(code) is False
    with pytest.raises(ValueError):
        REAL_SEGMENT_TRANSITION_MAP.check_valid_segment(code)
    with pytest.raises(ValueError):
        SegmentFactory.get_segment_state_machine(code)
