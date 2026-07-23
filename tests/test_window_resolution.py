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
from src.agent.edp.orchestrator import _resolve_post_trade_window_end, _resolve_window

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


# ---------------------------------------------------------------------------
# _resolve_post_trade_window_end() — the closing deadline for the 5 T+1
# post-trade processes. Without a real deadline here, a process CBOS never
# responds to would poll (BLOCKED) forever with no FAILED/TIMEOUT outcome
# and no alert ever firing (see AbstractStateMachine.execute_handler()'s
# window-deadline check, shared with real segments).
# ---------------------------------------------------------------------------


def test_post_trade_window_end_defaults_to_06_00_on_trade_date_plus_1():
    workflow_json = build_default_workflow_json(
        [],
        post_trade_processes=[{"process_code": "COLVAL", "login_id": "G_LID"}],
    )
    window_end = _resolve_post_trade_window_end("COLVAL", workflow_json, TRADE_DATE, IST)

    assert window_end.date() == date(2999, 1, 2)
    assert (window_end.hour, window_end.minute) == (6, 0)


def test_post_trade_window_end_override_takes_priority_over_default():
    workflow_json = build_default_workflow_json(
        [],
        post_trade_processes=[
            {"process_code": "COLVAL", "login_id": "G_LID", "window_end": "05:00"},
        ],
    )
    window_end = _resolve_post_trade_window_end("COLVAL", workflow_json, TRADE_DATE, IST)

    assert window_end.date() == date(2999, 1, 2)
    assert (window_end.hour, window_end.minute) == (5, 0)


def test_post_trade_window_end_override_on_one_process_does_not_affect_another():
    workflow_json = build_default_workflow_json(
        [],
        post_trade_processes=[
            {"process_code": "COLVAL", "login_id": "G_LID", "window_end": "05:00"},
            {"process_code": "COLALLOC", "login_id": "G_LID"},
        ],
    )
    colalloc_end = _resolve_post_trade_window_end("COLALLOC", workflow_json, TRADE_DATE, IST)

    assert (colalloc_end.hour, colalloc_end.minute) == (6, 0), (
        "COLVAL's override must not leak into COLALLOC's default deadline"
    )
