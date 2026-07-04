"""
Day 2 — failure path.

EQ's second process (BILLPOSTING — process order=2, right after fileupload,
per the process ordering documented in models.EdpProperties) returns a
permanent CBOS error. Expect:
  - EQ ends FAILED (not SKIPPED — a permanent CBOS error halts the day,
    unlike TIMEOUT/CBOS_SKIP which just skip that one segment and move on).
  - Every segment after EQ in sequence order, plus the virtual MTFOPS chain,
    stays untouched at PENDING — the orchestrator halts the sequential
    chain at the first FAILED segment (orchestrator.run_wake_cycle).
  - Ops can recover via repository.retry_segment (the same operation the
    POST /edp/status/{date}/{segment}/retry endpoint performs) and the day
    can then finish end to end once the underlying CBOS issue is fixed.
"""

from __future__ import annotations

from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.repository import get_day_summary
from src.agent.edp.utils.constants import MTF_OPS_SEGMENT_CODE, SEGMENT_ORDER
from src.tools.cbos_client import CbosClient

from . import helpers
from .fakes import FailingCbosClient

FAILING_SEGMENT = "EQ"
FAILING_PROCESS = "BILLPOSTING"  # 2nd process (order=2) in the per-segment pipeline


async def test_second_process_failure_halts_the_day(cfg, session_factory, test_date):
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
    # _fail() freezes current_phase exactly where the pipeline broke —
    # unlike _skip()/_complete(), it does NOT force it to DONE.
    assert failing_row.current_phase == SegmentPhase.AWAIT_BILLPOSTING
    assert failing_row.completed_at is not None
    assert failing_row.started_at is not None

    # --- Everything after EQ in sequence order must be untouched ---
    idx = SEGMENT_ORDER.index(FAILING_SEGMENT)
    untouched_codes = list(SEGMENT_ORDER[idx + 1:]) + [MTF_OPS_SEGMENT_CODE]
    assert untouched_codes, "test assumes EQ is not the last real segment"
    for code in untouched_codes:
        row = by_code[code]
        assert row.segment_status == SegmentStatus.PENDING, (
            f"segment {code} expected PENDING (chain halted at {FAILING_SEGMENT}), "
            f"got {row.segment_status}"
        )
        assert row.current_phase is None
        assert row.started_at is None
        assert row.completed_at is None

    # --- Day summary must reflect exactly one FAILED, rest PENDING ---
    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)
    assert summary["total"] == 9
    assert summary["failed"] == 1
    assert summary["pending"] == 8
    assert summary["completed"] == 0
    assert summary["in_progress"] == 0
    assert summary["skipped"] == 0


async def test_manual_retry_then_day_completes(cfg, session_factory, test_date):
    """
    Ops-facing recovery: once the underlying CBOS issue is resolved,
    retry_segment() resets the FAILED segment to a clean PENDING row and
    the day can run to completion on the next pass.
    """
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
    assert retried.lock_json["state"] == "UNLOCKED"

    # Ops fixed the CBOS-side issue -> swap in a healthy client and re-drive.
    healthy_cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    healthy_cbos.mock_set_ready_after(1)
    orchestrator.cbos = healthy_cbos

    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    for code in list(SEGMENT_ORDER) + [MTF_OPS_SEGMENT_CODE]:
        assert by_code[code].segment_status == SegmentStatus.COMPLETED, (
            f"segment {code} expected COMPLETED after retry, got {by_code[code].segment_status}"
        )
