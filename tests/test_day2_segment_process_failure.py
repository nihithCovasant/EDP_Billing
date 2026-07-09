"""
Day 2 — failure path.

EQ's BILLPOSTING process returns a permanent CBOS error. Expect EQ to end
FAILED while every other segment, processed independently, completes
normally. Ops can then recover via repository.retry_segment().
"""

from __future__ import annotations

from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.repository import get_day_summary
from src.agent.edp.utils.constants import SEGMENT_ORDER
from src.tools.cbos_client import CbosClient

from . import helpers
from .fakes import FailingCbosClient

FAILING_SEGMENT = "EQ"
FAILING_PROCESS = "BILLPOSTING"  # 2nd process (order=2) in the per-segment pipeline


async def test_second_process_failure_does_not_block_other_segments(cfg, session_factory, test_date):
    cbos = FailingCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url,
        fail_segment=FAILING_SEGMENT, fail_process=FAILING_PROCESS,
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    # --- The failing segment itself ---
    failing_row = by_code[FAILING_SEGMENT]
    assert failing_row.segment_status == SegmentStatus.FAILED
    assert failing_row.skip_category == "CBOS_ERROR"
    assert FAILING_PROCESS in failing_row.skip_reason
    assert failing_row.current_phase == SegmentPhase.AWAIT_BILLPOSTING
    assert failing_row.completed_at is not None
    assert failing_row.started_at is not None

    # --- Every other segment is independent of EQ and completes normally ---
    other_codes = [c for c in SEGMENT_ORDER if c != FAILING_SEGMENT]
    assert other_codes, "test assumes EQ is not the only segment"
    assert "MF" in other_codes, "MF is just a normal segment, unaffected by EQ's failure"
    for code in other_codes:
        row = by_code[code]
        assert row.segment_status == SegmentStatus.COMPLETED, (
            f"segment {code} expected COMPLETED (independent of {FAILING_SEGMENT}'s failure), "
            f"got {row.segment_status}"
        )

    # --- Day summary must reflect exactly one FAILED, rest COMPLETED ---
    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)
    assert summary["total"] == len(SEGMENT_ORDER)
    assert summary["failed"] == 1
    assert summary["completed"] == len(SEGMENT_ORDER) - 1
    assert summary["pending"] == 0
    assert summary["in_progress"] == 0
    assert summary["skipped"] == 0


async def test_manual_retry_then_day_completes(cfg, session_factory, test_date):
    """retry_segment() resets a FAILED segment to PENDING so the day can
    run to completion once the underlying CBOS issue is resolved."""
    failing_cbos = FailingCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url,
        fail_segment=FAILING_SEGMENT, fail_process=FAILING_PROCESS,
    )
    failing_cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, failing_cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    assert by_code[FAILING_SEGMENT].segment_status == SegmentStatus.FAILED

    async with session_factory() as session:
        retried = await repository.retry_segment(session, test_date, FAILING_SEGMENT)
        await session.commit()

    assert retried is not None
    assert retried.segment_status == SegmentStatus.PENDING
    assert retried.current_phase is None
    assert retried.skip_category is None
    assert retried.skip_reason is None
    assert retried.processes_json == {}

    # Ops fixed the CBOS-side issue -> swap in a healthy client and re-drive.
    healthy_cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    healthy_cbos.mock_set_ready_after(1)
    orchestrator.cbos = healthy_cbos

    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    for code in SEGMENT_ORDER:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED, (
            f"segment {code} expected COMPLETED after retry, got {by_code[code].segment_status}"
        )
