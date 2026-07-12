"""
Unit tests for the pure datetime helpers in
src/agent/edp/utils/datetime_utils.py — no DB, no CBOS, no asyncio.

- parse_window_dt()'s next_day flag is what distinguishes an overnight
  window (e.g. 17:30 trade_date -> 04:00 trade_date+1) from a same-day
  one; getting the date arithmetic wrong here would silently shift every
  overnight segment's window by a day.
- ensure_aware() is deliberately a "replace(tzinfo=...)", NOT an
  "astimezone(...)" — the former just labels a naive datetime's existing
  wall-clock time as being in `tz`, the latter would convert the clock
  value across timezones. Mixing these up would corrupt any naive
  datetime that reaches this function (e.g. from a DB driver that drops
  tzinfo), so the tests assert the wall-clock fields are preserved
  exactly rather than converted.
- resolve_active_date() is the "before cutoff_hour IST, still processing
  yesterday" rule the whole day-rollover logic depends on; its boundary
  behavior (< vs <=) and its cross-timezone conversion (comparing in
  tz_name, not in now's original tz) are both easy to get backwards, so
  each is tested explicitly with a case that would fail for the wrong
  implementation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.agent.edp.utils.datetime_utils import (
    IST,
    ensure_aware,
    now_ist,
    parse_window_dt,
    resolve_active_date,
)


def test_now_ist_returns_aware_datetime_in_ist():
    dt = now_ist()
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)


def test_parse_window_dt_same_day():
    dt = parse_window_dt(date(2026, 6, 29), "17:30", next_day=False, tz=IST)
    assert dt == datetime(2026, 6, 29, 17, 30, tzinfo=IST)


def test_parse_window_dt_next_day_advances_date_by_exactly_one():
    dt = parse_window_dt(date(2026, 6, 29), "17:30", next_day=True, tz=IST)
    assert dt.date() == date(2026, 6, 30)
    assert dt.hour == 17 and dt.minute == 30
    assert dt.tzinfo is IST


def test_parse_window_dt_midnight_same_day_no_off_by_one():
    dt = parse_window_dt(date(2026, 6, 29), "00:00", next_day=False, tz=IST)
    assert dt == datetime(2026, 6, 29, 0, 0, tzinfo=IST)


def test_parse_window_dt_midnight_next_day_no_off_by_one():
    dt = parse_window_dt(date(2026, 6, 29), "00:00", next_day=True, tz=IST)
    assert dt == datetime(2026, 6, 30, 0, 0, tzinfo=IST)


def test_ensure_aware_none_returns_none():
    assert ensure_aware(None) is None


def test_ensure_aware_already_aware_datetime_is_unchanged():
    """Must not silently convert timezones — an already-aware datetime in
    some other zone should come back untouched, same tzinfo and instant."""
    utc_dt = datetime(2026, 6, 29, 10, 0, tzinfo=ZoneInfo("UTC"))
    result = ensure_aware(utc_dt)
    assert result is utc_dt
    assert result.tzinfo is utc_dt.tzinfo
    assert result.hour == 10


def test_ensure_aware_naive_datetime_gets_tz_attached_not_converted():
    """This is a .replace(tzinfo=...), not .astimezone(...): the wall-clock
    hour/minute/second must stay exactly as given, just labeled as IST."""
    naive = datetime(2026, 6, 29, 14, 45, 30)
    result = ensure_aware(naive)
    assert result.tzinfo is IST
    assert result.hour == 14
    assert result.minute == 45
    assert result.second == 30


def test_ensure_aware_naive_datetime_default_tz_is_ist():
    naive = datetime(2026, 6, 29, 14, 45)
    result = ensure_aware(naive)
    assert result.tzinfo is IST


def test_ensure_aware_naive_datetime_custom_tz():
    naive = datetime(2026, 6, 29, 14, 45)
    utc = ZoneInfo("UTC")
    result = ensure_aware(naive, tz=utc)
    assert result.tzinfo is utc
    assert result.hour == 14
    assert result.minute == 45


def test_resolve_active_date_before_cutoff_is_previous_day():
    """Docstring example: 04:30 IST on 29-Jun with cutoff_hour=6 ->
    active_date = 28-Jun."""
    now = datetime(2026, 6, 29, 4, 30, tzinfo=IST)
    assert resolve_active_date(now, cutoff_hour=6, tz_name="Asia/Kolkata") == date(2026, 6, 28)


def test_resolve_active_date_well_after_cutoff_is_same_day():
    now = datetime(2026, 6, 29, 14, 0, tzinfo=IST)
    assert resolve_active_date(now, cutoff_hour=6, tz_name="Asia/Kolkata") == date(2026, 6, 29)


def test_resolve_active_date_exactly_at_cutoff_hour_boundary():
    """Code uses `local.hour < cutoff_hour`, so hour == cutoff_hour is NOT
    "before cutoff" — it resolves to the same calendar day."""
    now = datetime(2026, 6, 29, 6, 0, tzinfo=IST)
    assert resolve_active_date(now, cutoff_hour=6, tz_name="Asia/Kolkata") == date(2026, 6, 29)


def test_resolve_active_date_converts_from_a_different_timezone_before_comparing():
    """
    23:30 UTC on 28-Jun is 05:00 IST on 29-Jun (UTC+5:30) — still before a
    cutoff_hour=6 IST comparison, so active_date should resolve to 28-Jun
    (the IST calendar day minus one), NOT to 28-Jun purely by coincidence
    of the UTC date: if the code wrongly compared `now.hour` (23) against
    cutoff_hour in its original tz instead of astimezone-converting to
    tz_name first, it would take the ">= cutoff" branch and return
    28-Jun's UTC date directly rather than deriving 29-Jun's IST date and
    subtracting a day. Both paths coincidentally land on 28-Jun here, so
    the second case below (which the two approaches disagree on) is the
    real proof of correct conversion.
    """
    now_utc = datetime(2026, 6, 28, 23, 30, tzinfo=ZoneInfo("UTC"))
    assert resolve_active_date(now_utc, cutoff_hour=6, tz_name="Asia/Kolkata") == date(2026, 6, 28)

    # 01:00 UTC on 29-Jun -> 06:30 IST on 29-Jun: at/after cutoff_hour=6 in
    # IST, so active_date = 29-Jun. Comparing naively against the UTC hour
    # (1 < 6) would wrongly conclude "before cutoff" and return 28-Jun.
    now_utc_2 = datetime(2026, 6, 29, 1, 0, tzinfo=ZoneInfo("UTC"))
    assert resolve_active_date(now_utc_2, cutoff_hour=6, tz_name="Asia/Kolkata") == date(2026, 6, 29)
