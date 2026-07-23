"""
Boundary Value Analysis (BVA): test exact limits (n-1, n, n+1) rather than
mid-range values, since off-by-one errors live at equivalence-class edges.

Two targets:

1. AbstractSegmentStateMachine.is_my_time_window() / is_my_window_over().
   Deliberately asymmetric:

       is_my_time_window(now, window_start) -> now >= window_start   (INCLUSIVE)
       is_my_window_over(now, window_end)   -> now > window_end      (EXCLUSIVE)

   Together this makes [window_start, window_end] a fully-inclusive closed
   interval: at now == window_start the window is open (runnable exactly
   AT the start time), and at now == window_end it's not yet over (the
   deadline instant itself is still valid, not the first invalid one).
   Tests 4-6 below prove this precisely.

2. cbos_client._is_transient_http_status(). tests/test_cbos_client_parsing.py
   already covers 429/500/503 (transient) and 400/404 (permanent). This
   file adds the numeric boundaries between adjacent classes: 399/400,
   428/429/430, 499/500, 599/600, and the must-not-crash 2xx/3xx/negative/
   huge inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agent.edp.state_machine.AbstractStateMachine import AbstractSegmentStateMachine
from src.tools.cbos_client import _is_transient_http_status

# ---------------------------------------------------------------------------
# Target 1 — is_my_time_window() / is_my_window_over() boundary asymmetry
# ---------------------------------------------------------------------------

# is_my_time_window/is_my_window_over are plain methods that don't touch
# self, so a minimal concrete subclass with no real transition_map is
# enough to instantiate and call them directly.


class _MinimalStateMachine(AbstractSegmentStateMachine):
    SEGMENT_CODE = "TEST"

    def get_state_handler(self, state):
        return None


@pytest.fixture
def sm() -> AbstractSegmentStateMachine:
    return _MinimalStateMachine(transition_map=None)


_ANCHOR = datetime(2026, 7, 11, 9, 0, 0, 0, tzinfo=UTC)
_ONE_US = timedelta(microseconds=1)


def test_now_equal_to_window_start_is_inside_the_window(sm):
    """
    BVA boundary n: now == window_start exactly (same microsecond, not
    "a moment after"). is_my_time_window uses `>=`, so the opening instant
    itself must already count as inside the window.
    """
    window_start = _ANCHOR
    now = datetime(2026, 7, 11, 9, 0, 0, 0, tzinfo=UTC)
    assert now == window_start  # sanity: genuinely identical instants
    assert sm.is_my_time_window(now, window_start) is True


def test_now_one_microsecond_before_window_start_is_outside_the_window(sm):
    """BVA boundary n-1: one microsecond before window_start must be False."""
    window_start = _ANCHOR
    now = window_start - _ONE_US
    assert sm.is_my_time_window(now, window_start) is False


def test_now_one_microsecond_after_window_start_is_inside_the_window(sm):
    """BVA boundary n+1: one microsecond after window_start must be True."""
    window_start = _ANCHOR
    now = window_start + _ONE_US
    assert sm.is_my_time_window(now, window_start) is True


def test_now_equal_to_window_end_is_not_yet_over(sm):
    """
    BVA boundary n: now == window_end exactly. is_my_window_over uses a
    STRICT `>`, so the exact deadline instant itself is NOT over yet — the
    most bug-prone boundary here, since "the window is over at its end
    time" is the intuitive (and wrong, per this code) reading. This test
    proves the actual (exclusive) behavior; see the module docstring above
    for why this reads as intentional design (an inclusive [start, end]
    interval) rather than an accidental off-by-one.
    """
    window_end = _ANCHOR
    now = datetime(2026, 7, 11, 9, 0, 0, 0, tzinfo=UTC)
    assert now == window_end  # sanity: genuinely identical instants
    assert sm.is_my_window_over(now, window_end) is False


def test_now_one_microsecond_after_window_end_is_over(sm):
    """BVA boundary n+1: one microsecond past window_end must flip to True."""
    window_end = _ANCHOR
    now = window_end + _ONE_US
    assert sm.is_my_window_over(now, window_end) is True


def test_now_one_microsecond_before_window_end_is_not_over(sm):
    """BVA boundary n-1: one microsecond before window_end must be False."""
    window_end = _ANCHOR
    now = window_end - _ONE_US
    assert sm.is_my_window_over(now, window_end) is False


def test_no_window_start_configured_means_always_open(sm):
    """Documented fallback: window_start=None -> always open, regardless of now."""
    assert sm.is_my_time_window(_ANCHOR, None) is True
    assert sm.is_my_time_window(_ANCHOR - timedelta(days=100), None) is True
    assert sm.is_my_time_window(_ANCHOR + timedelta(days=100), None) is True


def test_no_window_end_configured_means_never_over(sm):
    """Documented fallback: window_end=None -> never over, regardless of now."""
    assert sm.is_my_window_over(_ANCHOR, None) is False
    assert sm.is_my_window_over(_ANCHOR - timedelta(days=100), None) is False
    assert sm.is_my_window_over(_ANCHOR + timedelta(days=100), None) is False


# ---------------------------------------------------------------------------
# Target 2 — _is_transient_http_status() equivalence-class boundaries
# ---------------------------------------------------------------------------
# Implementation under test: `return status_code >= 500 or status_code == 429`
# Existing tests/test_cbos_client_parsing.py already covers representative
# members (429, 500, 503, 400, 404) — this file covers ONLY the untested
# numeric edges between classes.


def test_boundary_399_last_non_client_error_is_permanent():
    """399 is one below the first 4xx client-error code; not >=500, not 429."""
    assert _is_transient_http_status(399) is False


def test_boundary_400_first_client_error_is_permanent():
    """400 is the first client-error code; not >=500, not 429."""
    assert _is_transient_http_status(400) is False


def test_boundary_428_just_below_rate_limit_special_case_is_permanent():
    """
    428 (Precondition Required) is immediately below the 429 special case.
    Only status_code == 429 is special-cased transient, so 428 must be
    permanent even though it's directly adjacent to 429.
    """
    assert _is_transient_http_status(428) is False


def test_boundary_429_rate_limit_special_case_is_transient():
    """429 itself: the special-cased transient code (already covered in
    test_cbos_client_parsing.py; repeated here as the center of this
    3-point boundary group for completeness)."""
    assert _is_transient_http_status(429) is True


def test_boundary_430_just_above_rate_limit_special_case_is_permanent():
    """
    430 is not a registered HTTP status and not >=500 and not == 429, so it
    must fall back to permanent. Confirms the == 429 check is a single-point
    special case, not a range (e.g. not "in 429..430").
    """
    assert _is_transient_http_status(430) is False


def test_boundary_499_last_client_error_is_permanent():
    """499 (last code before the 5xx server-error class) must be permanent."""
    assert _is_transient_http_status(499) is False


def test_boundary_500_first_server_error_is_transient():
    """500 is the first code satisfying `>= 500`; must flip to transient."""
    assert _is_transient_http_status(500) is True


def test_boundary_599_last_valid_server_error_is_transient():
    """599 is the conventional top of the registered HTTP status range and
    still satisfies `>= 500`, so it must be transient."""
    assert _is_transient_http_status(599) is True


def test_out_of_spec_600_does_not_crash_and_is_classified_transient():
    """
    600 is one past the valid HTTP status range (100-599 by convention).
    The function does not validate range membership — it's a bare `>= 500`
    comparison — so it does NOT crash on this out-of-spec input.
    # BUG (arguable): 600 gets silently classified as "transient" purely
    # because `600 >= 500` is arithmetically true, even though no such HTTP
    # status is ever legitimately issued by CBOS. This is defensible (an
    # unrecognized code defaulting to "retry, don't hard-fail" is a safe
    # direction to err in) but it is not a deliberate design decision
    # documented anywhere near the function — it's a side effect of doing
    # a bare numeric comparison with no upper bound / no explicit
    # "unknown status" branch.
    """
    assert _is_transient_http_status(600) is True


def test_out_of_spec_999_does_not_crash_and_is_classified_transient():
    """
    999 is a common placeholder/garbage status code seen from broken
    proxies or misbehaving servers. Same non-crashing, `>= 500`-driven
    behavior as 600 above.
    # BUG (arguable): as with 600, 999 is treated as transient purely by
    # arithmetic coincidence, not because anyone decided garbage codes
    # should retry. Flagging here rather than normalizing it away.
    """
    assert _is_transient_http_status(999) is True


def test_out_of_spec_negative_status_does_not_crash_and_is_classified_permanent():
    """
    A negative status code (e.g. from a client-side transport error being
    miscoded as a status) is nonsensical, but the function must not crash
    on it. -1 is neither >=500 nor ==429, so it falls to permanent — this
    is the safe direction for a value that can never legitimately be a
    real HTTP status.
    """
    assert _is_transient_http_status(-1) is False


def test_boundary_2xx_success_range_called_directly_is_permanent_and_does_not_crash():
    """
    _is_transient_http_status is only ever invoked by callers after they've
    already confirmed a non-2xx status (see cbos_client.py call sites, all
    guarded by `if resp.status_code != 200`), so 200/299 should never reach
    it in the real flow. A defensive function shouldn't crash regardless —
    confirming it returns a sane (non-transient) answer if ever misused.
    """
    assert _is_transient_http_status(200) is False
    assert _is_transient_http_status(299) is False


def test_boundary_3xx_redirect_range_called_directly_is_permanent_and_does_not_crash():
    """Same rationale as the 2xx case directly above: 300 is a redirect
    status that should never reach this function in normal flow, but it
    must not crash or misbehave if it ever does."""
    assert _is_transient_http_status(300) is False
