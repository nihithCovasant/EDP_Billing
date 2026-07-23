"""
EDP-DEF-052 (Coverage Gap) — closest real analogue in this codebase to
"threshold-breach detection". There is no implemented feature that watches
for missing files by a deadline and escalates to Ops/IT (see orchestrator.py
and constants.py — no such logic exists), so that part of EDP-DEF-052 is a
product/scope gap, not a test gap, and is out of scope for this file.

What DOES exist and was previously untested is the orchestrator's own
diagnostic "Segment heartbeat STALE" warning (run_wake_cycle(),
orchestrator.py:162-174) — logged, nothing persisted, no alert dispatched —
which fires when an IN_PROGRESS segment's last_heartbeat_at is older than
STALE_HEARTBEAT_THRESHOLD. The equivalent API-facing signal (_runtime_health
-> "STALE") already has full unit coverage in
tests/unit_tests/test_serializers.py; this file closes the matching gap for
the orchestrator's own log line, driven through a real run_wake_cycle() call
(same wall-clock-patch technique as
test_midnight_rollover_trade_date_integrity.py's Scenario 2).
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
import logging

from src.agent.edp import repository
from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import STALE_HEARTBEAT_THRESHOLD
from src.tools.cbos_client import CbosClient

from .test_midnight_rollover_trade_date_integrity import _FixedDatetime, _patch_wall_clock, IST
from .. import helpers


async def _mark_in_progress_with_heartbeat(session_factory, trade_date, segment_code, heartbeat_at):
    async with session_factory() as session:
        row = await repository.get_one(session, trade_date, segment_code)
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.current_state = SegmentState.WAITING_FOR_FILE_UPLOAD
        row.last_heartbeat_at = heartbeat_at
        await session.commit()


async def test_stale_heartbeat_warning_fires_when_in_progress_past_threshold(
    cfg, session_factory, test_date, monkeypatch, caplog,
):
    """An IN_PROGRESS segment whose last heartbeat is older than
    STALE_HEARTBEAT_THRESHOLD must produce the "Segment heartbeat STALE"
    warning on the very next run_wake_cycle() pass."""
    trade_date = test_date
    await helpers.seed_day(session_factory, trade_date, cfg)

    now = datetime.combine(trade_date, dtime(12, 0), tzinfo=IST)
    stale_heartbeat = now - STALE_HEARTBEAT_THRESHOLD - timedelta(minutes=5)
    await _mark_in_progress_with_heartbeat(session_factory, trade_date, "EQ", stale_heartbeat)

    _patch_wall_clock(monkeypatch, now)
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)

    with caplog.at_level(logging.WARNING):
        await orchestrator.run_wake_cycle()

    matches = [
        r for r in caplog.records
        if "Segment heartbeat STALE" in r.message and f"segment=EQ" in r.message
        and f"trade_date={trade_date}" in r.message
    ]
    assert matches, (
        f"expected a 'Segment heartbeat STALE' warning for EQ/{trade_date}, "
        f"got warnings: {[r.message for r in caplog.records if r.levelno >= logging.WARNING]}"
    )


async def test_stale_heartbeat_warning_does_not_fire_with_recent_heartbeat(
    cfg, session_factory, test_date, monkeypatch, caplog,
):
    """Same IN_PROGRESS state, but the heartbeat is recent (well within
    STALE_HEARTBEAT_THRESHOLD) — no STALE warning should be logged."""
    trade_date = test_date
    await helpers.seed_day(session_factory, trade_date, cfg)

    now = datetime.combine(trade_date, dtime(12, 0), tzinfo=IST)
    recent_heartbeat = now - timedelta(minutes=1)
    await _mark_in_progress_with_heartbeat(session_factory, trade_date, "EQ", recent_heartbeat)

    _patch_wall_clock(monkeypatch, now)
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)

    with caplog.at_level(logging.WARNING):
        await orchestrator.run_wake_cycle()

    matches = [r for r in caplog.records if "Segment heartbeat STALE" in r.message]
    assert not matches, f"unexpected STALE warning(s) with a recent heartbeat: {[r.message for r in matches]}"


async def test_stale_heartbeat_warning_does_not_fire_for_non_in_progress_segment(
    cfg, session_factory, test_date, monkeypatch, caplog,
):
    """A COMPLETED segment with a very old heartbeat must never be flagged
    STALE — the check requires IN_PROGRESS, mirroring
    test_serializers.py's equivalent _runtime_health guard."""
    trade_date = test_date
    await helpers.seed_day(session_factory, trade_date, cfg)

    now = datetime.combine(trade_date, dtime(12, 0), tzinfo=IST)
    ancient_heartbeat = now - STALE_HEARTBEAT_THRESHOLD - timedelta(days=1)
    async with session_factory() as session:
        row = await repository.get_one(session, trade_date, "EQ")
        row.segment_status = SegmentStatus.COMPLETED
        row.last_heartbeat_at = ancient_heartbeat
        await session.commit()

    _patch_wall_clock(monkeypatch, now)
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    orchestrator = EdpOrchestrator(cfg, cbos)

    with caplog.at_level(logging.WARNING):
        await orchestrator.run_wake_cycle()

    matches = [r for r in caplog.records if "Segment heartbeat STALE" in r.message]
    assert not matches, f"COMPLETED segment must never be flagged STALE: {[r.message for r in matches]}"
