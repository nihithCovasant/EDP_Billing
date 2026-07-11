"""Pure datetime utilities — no DB, no CBOS dependencies."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_window_dt(
    trade_date: date,
    time_str: str,
    next_day: bool,
    tz: ZoneInfo,
) -> datetime:
    """
    Build a timezone-aware datetime from a HH:MM string on a given date.
    If next_day=True, advances the result by one calendar day (for overnight windows).
    """
    h, m = (int(x) for x in time_str.split(":"))
    dt = datetime.combine(trade_date, time(h, m), tzinfo=tz)
    if next_day:
        dt += timedelta(days=1)
    return dt


def ensure_aware(dt: datetime | None, tz: ZoneInfo = IST) -> datetime | None:
    """Defensive: attach tz to a naive datetime so comparisons work."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt


def resolve_active_date(now: datetime, cutoff_hour: int, tz_name: str) -> date:
    """
    Determine today's trading date.
    Before cutoff_hour IST → still processing the previous calendar day's EDP.
    e.g. 04:30 IST on 29-Jun → active_date = 28-Jun
    """
    local = now.astimezone(ZoneInfo(tz_name))
    if local.hour < cutoff_hour:
        return (local - timedelta(days=1)).date()
    return local.date()
