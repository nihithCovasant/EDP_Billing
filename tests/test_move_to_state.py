"""
pipeline.stages.move_to_state() — the single centralized function every
phase advance goes through (see pipeline/stages.py's docstring on it).

Covers, in isolation, every behavior it promises:
  - real transition: mutates current_phase/current_process, stamps
    last_heartbeat_at with the caller's `now`, logs exactly one line, flushes.
  - guard: old_phase == new_phase is a true no-op — no mutation (not even
    the heartbeat), no log line, no flush-worthy change.
  - new_process has three distinct behaviors: default (leave untouched),
    explicit None (clear it), explicit value (set it) — this distinction
    matters because a few real call sites (post-trade TRIGGER_JOB ->
    AWAIT_CONFIRM) must preserve a value set by an earlier stage.
  - old_phase=None (a row that's never been through the pipeline yet) is
    handled without crashing.
  - the mutation is a real DB write, visible from a fresh session after
    commit, not just an in-memory attribute set.

Plus one full-pipeline integration test confirming the new
last_heartbeat_at-on-every-transition behavior actually shows up end to end
through the real orchestrator (not just when move_to_state is called
directly).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.pipeline.stages import move_to_state
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import SEGMENT_ORDER
from src.agent.edp.utils.datetime_utils import now_ist
from src.tools.cbos_client import CbosClient

from . import helpers

SEGMENT = "EQ"


async def test_move_to_state_transitions_stamps_heartbeat_and_logs(caplog, cfg, session_factory, test_date):
    caplog.set_level(logging.INFO, logger="cams_otel_lib")
    await helpers.seed_day(session_factory, test_date, cfg)
    now = now_ist()

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.current_phase = SegmentPhase.HOLIDAY_CHECK
        row.current_process = None
        await session.flush()

        await move_to_state(
            row, session, SegmentPhase.RESERVE_PID, now,
            "moved for test",
            new_process="SOME_PROC",
            extra_marker="xyz123",
        )
        await session.commit()

        assert row.current_phase == SegmentPhase.RESERVE_PID
        assert row.current_process == "SOME_PROC"
        assert row.last_heartbeat_at == now

    log_text = "\n".join(r.message for r in caplog.records)
    assert "moved for test" in log_text
    assert "HOLIDAY_CHECK" in log_text, "old phase must appear in the log line"
    assert "new_phase=RESERVE_PID" in log_text
    assert "extra_marker=xyz123" in log_text, "arbitrary caller context must be forwarded to the log line"


async def test_move_to_state_is_full_noop_when_phase_unchanged(caplog, cfg, session_factory, test_date):
    """old_phase == new_phase must not touch current_process, must not stamp
    last_heartbeat_at, and must not log anything — this is what makes it
    safe to call move_to_state defensively/unconditionally."""
    caplog.set_level(logging.INFO, logger="cams_otel_lib")
    await helpers.seed_day(session_factory, test_date, cfg)
    old_heartbeat = now_ist() - timedelta(hours=2)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.current_phase = SegmentPhase.RESERVE_PID
        row.current_process = "UNCHANGED_PROC"
        row.last_heartbeat_at = old_heartbeat
        await session.flush()

        caplog.clear()
        await move_to_state(
            row, session, SegmentPhase.RESERVE_PID, now_ist(),
            "should never be logged",
            new_process="SHOULD_NOT_BE_SET",
        )

        assert row.current_phase == SegmentPhase.RESERVE_PID
        assert row.current_process == "UNCHANGED_PROC", "no-op must not touch current_process either"
        assert row.last_heartbeat_at == old_heartbeat, "no-op must not stamp a new heartbeat"

    assert not any("should never be logged" in r.message for r in caplog.records)


async def test_move_to_state_default_new_process_leaves_existing_value_untouched(cfg, session_factory, test_date):
    """The default (no new_process kwarg) must preserve whatever
    current_process already held — used by post-trade's
    TRIGGER_JOB -> AWAIT_CONFIRM, which must not clobber the ProcessName
    resolved back in AWAIT_GTG."""
    await helpers.seed_day(session_factory, test_date, cfg)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.current_phase = SegmentPhase.AWAIT_BILLPOSTING
        row.current_process = "BILLPOSTING"
        await session.flush()

        await move_to_state(
            row, session, SegmentPhase.AWAIT_RECON, now_ist(),
            "advance without touching current_process",
        )

        assert row.current_phase == SegmentPhase.AWAIT_RECON
        assert row.current_process == "BILLPOSTING", "default must leave current_process exactly as it was"


async def test_move_to_state_explicit_none_clears_current_process(cfg, session_factory, test_date):
    await helpers.seed_day(session_factory, test_date, cfg)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.current_phase = SegmentPhase.AWAIT_CONTRACT_NOTE
        row.current_process = "CONTRACTNOTEGENERATION"
        await session.flush()

        await move_to_state(
            row, session, SegmentPhase.RESERVE_PID, now_ist(),
            "explicit clear",
            new_process=None,
        )

        assert row.current_process is None


async def test_move_to_state_explicit_value_sets_current_process(cfg, session_factory, test_date):
    await helpers.seed_day(session_factory, test_date, cfg)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.current_phase = SegmentPhase.RESERVE_PID
        row.current_process = None
        await session.flush()

        await move_to_state(
            row, session, SegmentPhase.AWAIT_FILE_UPLOAD, now_ist(),
            "explicit set",
            new_process="FILEUPLOAD",
        )

        assert row.current_process == "FILEUPLOAD"


async def test_move_to_state_from_none_old_phase_does_not_crash(caplog, cfg, session_factory, test_date):
    """A row that has never entered the pipeline (current_phase is None) is
    a legitimate old_phase — must not raise (guarded by
    `old_phase.value if old_phase else "UNKNOWN"`) and must still transition."""
    caplog.set_level(logging.INFO, logger="cams_otel_lib")
    await helpers.seed_day(session_factory, test_date, cfg)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.current_phase = None
        await session.flush()

        await move_to_state(
            row, session, SegmentPhase.HOLIDAY_CHECK, now_ist(),
            "first entry into the pipeline",
        )

        assert row.current_phase == SegmentPhase.HOLIDAY_CHECK

    log_text = "\n".join(r.message for r in caplog.records)
    assert "first entry into the pipeline" in log_text
    assert "stage=UNKNOWN" in log_text or "UNKNOWN" in log_text


async def test_move_to_state_persists_across_sessions(cfg, session_factory, test_date):
    """Not just an in-memory attribute set — a real, committed DB write."""
    await helpers.seed_day(session_factory, test_date, cfg)
    now = now_ist()

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.current_phase = SegmentPhase.HOLIDAY_CHECK
        await session.flush()

        await move_to_state(
            row, session, SegmentPhase.RESERVE_PID, now,
            "persisted transition",
            new_process="PERSISTED_PROC",
        )
        await session.commit()

    async with session_factory() as fresh_session:
        reloaded = await repository.get_one(fresh_session, test_date, SEGMENT)
        assert reloaded.current_phase == SegmentPhase.RESERVE_PID
        assert reloaded.current_process == "PERSISTED_PROC"
        assert reloaded.last_heartbeat_at is not None


async def test_full_day_run_stamps_heartbeat_via_move_to_state(cfg, session_factory, test_date):
    """End-to-end: driving a real day through the orchestrator must leave
    every segment's last_heartbeat_at populated purely as a side effect of
    move_to_state's transitions (previously only the separate
    repository.touch_heartbeat() BLOCKED-path calls ever set this field)."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)

    by_code = {r.segment_code: r for r in rows}
    assert set(by_code) == set(SEGMENT_ORDER)
    for code in SEGMENT_ORDER:
        row = by_code[code]
        assert row.segment_status == SegmentStatus.COMPLETED
        assert row.last_heartbeat_at is not None, f"{code} should have a heartbeat stamped by move_to_state"
