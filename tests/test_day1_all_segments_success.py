"""
Day 1 — happy path.

Every real trade segment (EQ, DR, CUR, SL, NCDEX, MCX, NSECOM, MF) plus the
virtual post-segment MTFOPS chain completes successfully in sequence order,
driven through the real orchestrator + pipeline + CbosClient in-process mock
(no network calls, fully deterministic).
"""

from __future__ import annotations

from src.agent.edp.models import SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.repository import get_day_summary
from src.agent.edp.utils.constants import MTF_OPS_SEGMENT_CODE, SEGMENT_ORDER, get_sequence_order
from src.agent.edp.utils.serializers import serialize_segment, serialize_segment_summary
from src.tools.cbos_client import CbosClient

from . import helpers


async def test_all_segments_and_mtfops_complete_successfully(cfg, session_factory, test_date):
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)  # every poll succeeds first try -> fastest happy path
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert set(by_code) == set(SEGMENT_ORDER) | {MTF_OPS_SEGMENT_CODE}

    for code in SEGMENT_ORDER:
        row = by_code[code]
        assert row.segment_status == SegmentStatus.COMPLETED, (
            f"segment {code} expected COMPLETED, got {row.segment_status} "
            f"(skip_category={row.skip_category!r} skip_reason={row.skip_reason!r})"
        )
        assert row.current_phase is not None and row.current_phase.value == "DONE"
        assert row.completed_at is not None
        assert row.started_at is not None
        assert row.skip_category is None
        assert row.skip_reason is None
        # processes_json should record every one of the 6 internal stages as done.
        for stage_key in (
            "holiday_check", "file_upload_ready", "trigger",
            "bill_posting", "recon", "contract_note",
        ):
            assert stage_key in row.processes_json, f"{code} missing processes_json[{stage_key}]"

    mtf_row = by_code[MTF_OPS_SEGMENT_CODE]
    assert mtf_row.segment_status == SegmentStatus.COMPLETED
    assert mtf_row.current_phase.value == "DONE"


async def test_segments_run_in_fixed_sequence_order(cfg, session_factory, test_date):
    """
    Segments must run in the fixed SEGMENT_ORDER code constant regardless of
    the order they were uploaded in workflow_json — sequence_order is no
    longer a stored/uploaded field (see utils/constants.get_sequence_order).
    """
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows_before = await helpers.get_rows(session_factory, test_date)
    ordered_codes = [r.segment_code for r in rows_before]
    assert ordered_codes == list(SEGMENT_ORDER) + [MTF_OPS_SEGMENT_CODE]

    rows_after = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    for row in rows_after:
        assert row.segment_status == SegmentStatus.COMPLETED


async def test_day_summary_and_serializers_have_no_removed_fields(cfg, session_factory, test_date):
    """
    Gap-check for the recent segment_execution simplification: the day
    summary and per-segment serializers must not leak domain/window_*_at
    columns that no longer exist on the model, and must compute
    segment_name/sequence_order/runtime_health/lock_state instead of
    reading them off stored columns.
    """
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)

    async with session_factory() as session:
        summary = await get_day_summary(session, test_date)

    assert summary["total"] == 9
    assert summary["completed"] == 9
    assert summary["pending"] == 0
    assert summary["in_progress"] == 0
    assert summary["skipped"] == 0
    assert summary["failed"] == 0
    assert "domain" not in summary

    eq_row = next(r for r in rows if r.segment_code == "EQ")
    detail = serialize_segment(eq_row)
    summary_row = serialize_segment_summary(eq_row)

    assert detail["segment_name"] == "Cash"
    assert detail["sequence_order"] == get_sequence_order("EQ") == 1
    assert detail["runtime_health"] == "ACTIVE"
    assert detail["lock_state"] == "UNLOCKED"
    assert detail["lock_owner"] is None
    for removed_field in ("domain", "window_start_at", "window_end_at"):
        assert removed_field not in detail
        assert removed_field not in summary_row
