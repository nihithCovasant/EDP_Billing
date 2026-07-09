"""
_resolve_window() is a pure function of (segment_code, workflow_json,
trade_date, tz) — no DB/orchestrator wiring needed to exercise it directly.

Segments are same-day. window_end only rolls onto trade_date+1 when it's
chronologically at/before window_start on trade_date (an overnight window
crossing midnight, e.g. 17:00 -> 06:00). A same-day window (e.g. a short
09:47 -> 09:48 slice) must resolve entirely within trade_date, even though
window_end has already passed by the time "now" is checked.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.orchestrator import _resolve_window

IST = ZoneInfo("Asia/Kolkata")
TRADE_DATE = date(2999, 1, 1)


def _workflow_json(window_start: str, window_end: str) -> dict:
    return build_default_workflow_json(
        [{"segment_code": "EQ", "login_id": "CV0001", "window_start": window_start, "window_end": window_end}]
    )


def test_same_day_window_stays_on_trade_date_when_end_is_after_start():
    workflow_json = _workflow_json("09:47", "09:48")
    window_start, window_end = _resolve_window("EQ", workflow_json, TRADE_DATE, IST)

    assert window_start.date() == TRADE_DATE
    assert window_end.date() == TRADE_DATE


def test_overnight_window_rolls_end_to_next_day_when_end_is_before_start():
    workflow_json = _workflow_json("17:00", "06:00")
    window_start, window_end = _resolve_window("EQ", workflow_json, TRADE_DATE, IST)

    assert window_start.date() == TRADE_DATE
    assert window_end.date() == date(2999, 1, 2)


def test_end_equal_to_start_rolls_to_next_day_rather_than_a_zero_width_window():
    workflow_json = _workflow_json("10:00", "10:00")
    window_start, window_end = _resolve_window("EQ", workflow_json, TRADE_DATE, IST)

    assert window_start.date() == TRADE_DATE
    assert window_end.date() == date(2999, 1, 2)
