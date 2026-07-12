"""
Boundary Value Analysis (BVA) for two time-related decision points:

- resolve_active_date()'s `local.hour < cutoff_hour` cutoff check
  (src/agent/edp/utils/datetime_utils.py) — this comparison is HOUR-ONLY
  granularity, not a full datetime comparison, so anything within the
  cutoff hour itself (e.g. 06:00:00.000000 through 06:59:59.999999 when
  cutoff_hour=6) resolves identically. These tests pin down exactly where
  the n-1/n/n+1 boundaries fall in real (year, month, day, hour, minute,
  second, microsecond) terms, and document what happens for out-of-spec
  cutoff_hour values (24, -1, 0), since neither resolve_active_date() nor
  EdpBootstrapConfig validates cutoff_hour's 0-23 range.

- _runtime_health()'s `(now_ist() - ensure_aware(last_heartbeat_at)) >
  STALE_HEARTBEAT_THRESHOLD` staleness check (src/agent/edp/utils/
  serializers.py) — strict `>` against a 10-minute constants.py threshold.
  These tests confirm the exact ACTIVE/STALE boundary and probe a
  clock-skew scenario (heartbeat timestamped slightly in the future
  relative to "now") for crash-safety.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.agent.edp.utils.constants import STALE_HEARTBEAT_THRESHOLD
from src.agent.edp.utils.datetime_utils import resolve_active_date
from src.agent.edp.utils.serializers import _runtime_health
from src.agent.edp.models import SegmentStatus

IST = ZoneInfo("Asia/Kolkata")


def _at(y, mo, d, h, mi, s, us=0, tz=IST) -> datetime:
    return datetime(y, mo, d, h, mi, s, us, tzinfo=tz)


# ---------------------------------------------------------------------------
# Target 1: resolve_active_date() cutoff-hour boundary
# ---------------------------------------------------------------------------


def test_exact_cutoff_hour_start_is_same_day():
    """06:00:00.000000 IST with cutoff_hour=6 — hour is NOT < 6, so this is
    the FIRST instant classified as same-day (the n boundary)."""
    now = _at(2026, 6, 29, 6, 0, 0, 0)
    assert resolve_active_date(now, 6, "Asia/Kolkata") == date(2026, 6, 29)


def test_one_microsecond_before_cutoff_is_previous_day():
    """05:59:59.999999 IST — the LAST instant still classified as
    previous-day (the n-1 boundary), one microsecond before the cutoff."""
    now = _at(2026, 6, 29, 5, 59, 59, 999999)
    assert resolve_active_date(now, 6, "Asia/Kolkata") == date(2026, 6, 28)


def test_hour_only_granularity_makes_whole_cutoff_hour_indistinguishable():
    """
    FINDING: the comparison is `local.hour < cutoff_hour` — hour-only, not
    a full datetime compare. So 06:00:00.000001 (one microsecond after the
    n boundary) and 06:59:59.999999 (the last instant before hour 7)
    resolve IDENTICALLY to 06:00:00.000000, because all three share
    hour == 6. Any minute/second/microsecond precision within the cutoff
    hour is invisible to this function. If ops ever expects a
    minute-level cutoff (e.g. "06:30 IST"), this is a real gap: the
    finest granularity resolve_active_date() can express is a whole hour.
    """
    now_start_of_hour = _at(2026, 6, 29, 6, 0, 0, 0)
    now_just_after = _at(2026, 6, 29, 6, 0, 0, 1)
    now_end_of_hour = _at(2026, 6, 29, 6, 59, 59, 999999)

    result_start = resolve_active_date(now_start_of_hour, 6, "Asia/Kolkata")
    result_just_after = resolve_active_date(now_just_after, 6, "Asia/Kolkata")
    result_end = resolve_active_date(now_end_of_hour, 6, "Asia/Kolkata")

    assert result_start == result_just_after == result_end == date(2026, 6, 29)


def test_cutoff_hour_zero_disables_rollover_entirely():
    """
    FINDING: with cutoff_hour=0, `local.hour < 0` can never be True (Python
    datetime.hour is always in 0-23), so the "previous calendar day"
    rollover branch is dead code for every hour of the day — active_date
    always equals the local calendar date. cutoff_hour=0 is therefore not
    "midnight cutoff" in the intuitive sense; it's "no cutoff at all."
    Neither resolve_active_date() nor EdpBootstrapConfig
    (src/agent/edp/config.py, active_date_cutoff_hour default=6) validates
    that cutoff_hour falls within 0-23 — there is no guard in either
    place, so this misconfiguration would silently change pipeline
    behavior rather than fail fast at startup.
    """
    for hour in range(24):
        now = _at(2026, 6, 29, hour, 0, 0, 0)
        assert resolve_active_date(now, 0, "Asia/Kolkata") == date(2026, 6, 29)


def test_cutoff_hour_23_near_max_normal_case():
    """cutoff_hour=23 is the highest in-spec value (hours run 0-23) — only
    hour 23 itself is same-day; every earlier hour rolls back."""
    just_before = _at(2026, 6, 29, 22, 59, 59, 999999)
    at_23 = _at(2026, 6, 29, 23, 0, 0, 0)

    assert resolve_active_date(just_before, 23, "Asia/Kolkata") == date(2026, 6, 28)
    assert resolve_active_date(at_23, 23, "Asia/Kolkata") == date(2026, 6, 29)


def test_out_of_spec_cutoff_hour_24_rolls_back_every_hour():
    """
    FINDING (out-of-spec input, does NOT crash): cutoff_hour=24 makes
    `local.hour < 24` always True, since hour is always 0-23. Every hour
    of the day is therefore treated as "before cutoff," so active_date is
    ALWAYS the previous calendar day, no matter what time `now` is. This
    is silently nonsensical, not an exception — confirmed here rather
    than assumed.
    """
    for hour in (0, 6, 12, 23):
        now = _at(2026, 6, 29, hour, 0, 0, 0)
        assert resolve_active_date(now, 24, "Asia/Kolkata") == date(2026, 6, 28)


def test_out_of_spec_cutoff_hour_negative_one_behaves_like_zero():
    """
    FINDING (out-of-spec input, does NOT crash): cutoff_hour=-1 makes
    `local.hour < -1` always False, since hour is always >= 0. This is
    behaviorally identical to cutoff_hour=0 — no rollover ever happens,
    active_date always equals the local calendar date.
    """
    for hour in (0, 6, 12, 23):
        now = _at(2026, 6, 29, hour, 0, 0, 0)
        assert resolve_active_date(now, -1, "Asia/Kolkata") == date(2026, 6, 29)


# ---------------------------------------------------------------------------
# Target 2: STALE_HEARTBEAT_THRESHOLD boundary in _runtime_health()
# ---------------------------------------------------------------------------


def _fake_row(last_heartbeat_at) -> SimpleNamespace:
    """Minimal stand-in for SegmentExecution — _runtime_health() only reads
    .segment_status and .last_heartbeat_at."""
    return SimpleNamespace(
        segment_status=SegmentStatus.IN_PROGRESS,
        last_heartbeat_at=last_heartbeat_at,
    )


def _frozen_now_ist(monkeypatch, frozen: datetime):
    """Monkeypatch serializers.now_ist() (the name _runtime_health() calls,
    imported via `from .datetime_utils import ensure_aware, now_ist`) to a
    fixed instant, so elapsed-time boundaries can be pinned exactly instead
    of racing the real clock between test setup and the function call."""
    import src.agent.edp.utils.serializers as serializers_mod

    monkeypatch.setattr(serializers_mod, "now_ist", lambda: frozen)


def test_heartbeat_age_exactly_at_threshold_is_still_active(monkeypatch):
    """
    Confirmed comparison operator is strict `>` (serializers.py:16):
    `... > STALE_HEARTBEAT_THRESHOLD`. Age exactly == 10:00 minutes is NOT
    `> 10 minutes`, so the segment is still ACTIVE at the n boundary.
    now_ist() is frozen via monkeypatch so the elapsed time is pinned
    exactly to the threshold rather than racing the real clock.
    """
    frozen_now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=IST)
    _frozen_now_ist(monkeypatch, frozen_now)

    row = _fake_row(frozen_now - STALE_HEARTBEAT_THRESHOLD)
    assert _runtime_health(row) == "ACTIVE"


def test_heartbeat_age_one_second_under_threshold_is_active(monkeypatch):
    """9:59 elapsed (threshold - 1 second) — comfortably ACTIVE."""
    frozen_now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=IST)
    _frozen_now_ist(monkeypatch, frozen_now)

    row = _fake_row(frozen_now - (STALE_HEARTBEAT_THRESHOLD - timedelta(seconds=1)))
    assert _runtime_health(row) == "ACTIVE"


def test_heartbeat_age_one_second_over_threshold_is_stale(monkeypatch):
    """10:01 elapsed (threshold + 1 second) — the first instant STALE."""
    frozen_now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=IST)
    _frozen_now_ist(monkeypatch, frozen_now)

    row = _fake_row(frozen_now - (STALE_HEARTBEAT_THRESHOLD + timedelta(seconds=1)))
    assert _runtime_health(row) == "STALE"


def test_heartbeat_age_zero_is_active(monkeypatch):
    """Heartbeat literally "now" — zero elapsed time, trivially ACTIVE."""
    frozen_now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=IST)
    _frozen_now_ist(monkeypatch, frozen_now)

    row = _fake_row(frozen_now)
    assert _runtime_health(row) == "ACTIVE"


def test_non_in_progress_status_is_always_active_regardless_of_heartbeat_age(monkeypatch):
    """Staleness only applies to IN_PROGRESS segments (serializers.py:14) —
    a COMPLETED segment with an ancient heartbeat must still read ACTIVE."""
    frozen_now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=IST)
    _frozen_now_ist(monkeypatch, frozen_now)

    row = SimpleNamespace(
        segment_status=SegmentStatus.COMPLETED,
        last_heartbeat_at=frozen_now - timedelta(days=1),
    )
    assert _runtime_health(row) == "ACTIVE"


def test_clock_skew_future_heartbeat_resolves_active_without_crashing(monkeypatch):
    """
    Robustness/clock-skew case: last_heartbeat_at is 5 seconds AHEAD of
    now_ist() (e.g. a writer node's clock is slightly fast). The
    subtraction `now_ist() - ensure_aware(last_heartbeat_at)` then yields
    a NEGATIVE timedelta. A negative timedelta compares `> positive
    threshold` as False in Python (no exception), so this must resolve to
    ACTIVE, not crash and not spuriously read STALE. Confirmed here rather
    than assumed.
    """
    frozen_now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=IST)
    _frozen_now_ist(monkeypatch, frozen_now)

    future_heartbeat = frozen_now + timedelta(seconds=5)
    row = _fake_row(future_heartbeat)

    result = _runtime_health(row)  # must not raise
    assert result == "ACTIVE"

    # Directly confirm the negative-timedelta comparison behavior in isolation:
    negative_elapsed = timedelta(seconds=-5)
    assert not (negative_elapsed > STALE_HEARTBEAT_THRESHOLD)
