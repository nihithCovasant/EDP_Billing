"""
Post-trade processes are now fully config-driven — pulled from the active
workflow_json's "post_trade_processes" list rather than fixed code
constants, in every scenario:

  1. Fetching  — GET /edp/workflow/{date} returns whatever was uploaded,
     including post_trade_processes as-is (see api/workflow.py).
  2. Consuming — the actual CBOS calls (file_process_status login_id +
     ProcessName, trigger login_id) use the config-resolved values, not the
     fixed defaults (tested here via RecordingFileStatusCbosClient).
  3. Initializing — build_default_workflow_json()'s auto-seed path builds a
     post_trade_processes section too (covered by test_config_loading.py /
     orchestrator's bootstrap path; also exercised implicitly by every
     other post-trade test via helpers.seed_post_trade_day()).
  4. Updating — a re-upload with a *different* post_trade_processes config
     (custom login_id/gtg_process_name, or an opening window moved to a
     different process) takes effect the same way a segment login_id/
     window change does — resolved live every cycle, not baked in at seed
     time (see orchestrator._resolve_post_trade_window() /
     _find_post_trade_cfg()).

process_code itself (and which CBOS trigger endpoint fires) stays a fixed
code mapping — there's no CBOS integration for an arbitrary 6th process —
but everything else about each of the 5 fixed processes is now ops-owned.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

from src.agent.edp import repository
from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient

from . import helpers
from .fakes import RecordingFileStatusCbosClient


async def test_post_trade_process_uses_configured_login_id_and_process_name(
    cfg, session_factory, test_date,
):
    """
    A post_trade_processes entry with a custom login_id + gtg_process_name
    for COLVAL must be exactly what's sent to CBOS's file_process_status —
    not the fixed "G_LID" / "CollateralValuation" defaults.
    """
    workflow_json = build_default_workflow_json(
        [],
        post_trade_processes=[
            {"process_code": "COLVAL", "login_id": "CUSTOM_LID", "gtg_process_name": "CustomColVal"},
            {"process_code": "COLALLOC", "login_id": "G_LID"},
            {"process_code": "MTFFT", "login_id": "G_LID"},
            {"process_code": "DMRPT", "login_id": "G_LID"},
            {"process_code": "DMSTMT", "login_id": "G_LID"},
        ],
    )
    async with session_factory() as session:
        await repository.upload(session, test_date, workflow_json, uploaded_by="test")
        await session.commit()
    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
        await repository.seed_post_trade_processes(session, workflow, test_date)
        await session.commit()

    cbos = RecordingFileStatusCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = helpers.fixed_post_trade_now_for(test_date, orchestrator._tz)

    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome in ("advanced", "completed", "blocked")

    assert cbos.calls, "expected at least one file_process_status call"
    segment, process_name, user_id = cbos.calls[0]
    assert segment == "COLVAL"
    assert process_name == "CustomColVal", "must use the configured gtg_process_name, not the fixed default"
    assert user_id == "CUSTOM_LID", "must use the configured login_id, not the fixed default"


async def test_post_trade_default_window_gate_applies_independently_per_process(
    cfg, session_factory, test_date,
):
    """
    The default 02:00 IST (T+1) gate applies to EVERY post-trade process
    that doesn't have its own explicit window_start — an override on one
    process (here: COLALLOC's 03:00) must have no effect on another
    process's (COLVAL's) default gate.
    """
    workflow_json = build_default_workflow_json(
        [],
        post_trade_processes=[
            {"process_code": "COLVAL", "login_id": "G_LID"},
            {
                "process_code": "COLALLOC", "login_id": "G_LID",
                "window_start": "03:00",
            },
            {"process_code": "MTFFT", "login_id": "G_LID"},
            {"process_code": "DMRPT", "login_id": "G_LID"},
            {"process_code": "DMSTMT", "login_id": "G_LID"},
        ],
    )
    async with session_factory() as session:
        await repository.upload(session, test_date, workflow_json, uploaded_by="test")
        await session.commit()
    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
        await repository.seed_post_trade_processes(session, workflow, test_date)
        await session.commit()

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)
    orchestrator._cycle_active_date = test_date

    # Before COLVAL's default 02:00 gate opens — must stay blocked, even
    # though COLALLOC's own override is a different, unrelated time.
    before_window = datetime.combine(
        test_date + timedelta(days=1), dtime(0, 30), tzinfo=orchestrator._tz
    )
    orchestrator._cycle_now = before_window
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "blocked", "COLVAL must still default-gate at 02:00 T+1"

    # Past 02:00 T+1 — COLVAL proceeds normally.
    orchestrator._cycle_now = helpers.fixed_post_trade_now_for(test_date, orchestrator._tz)
    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome in ("advanced", "completed")


async def test_seed_post_trade_processes_skips_unknown_process_code(cfg, session_factory, test_date):
    """An unrecognized process_code in config must be skipped (with a
    warning), not crash the seeding pass — the other 5 known ones still get
    seeded normally."""
    workflow_json = build_default_workflow_json(
        [],
        post_trade_processes=[
            {"process_code": "NOT_A_REAL_CODE", "login_id": "G_LID"},
            {"process_code": "COLVAL", "login_id": "G_LID"},
        ],
    )
    async with session_factory() as session:
        await repository.upload(session, test_date, workflow_json, uploaded_by="test")
        await session.commit()

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
        created = await repository.seed_post_trade_processes(session, workflow, test_date)
        await session.commit()

    assert [r.segment_code for r in created] == ["COLVAL"]


async def test_legacy_workflow_without_post_trade_processes_key_still_seeds_fixed_five(
    cfg, session_factory, test_date,
):
    """A workflow_json uploaded before this feature existed (no
    "post_trade_processes" key at all) must still seed the fixed 5 —
    backward compatibility for already-uploaded configs."""
    legacy_workflow_json = {
        "segments": [],
        # deliberately no "post_trade_processes" key
    }
    async with session_factory() as session:
        await repository.upload(session, test_date, legacy_workflow_json, uploaded_by="test")
        await session.commit()

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
        created = await repository.seed_post_trade_processes(session, workflow, test_date)
        await session.commit()

    from src.agent.edp.utils.constants import POST_TRADE_ORDER
    assert {r.segment_code for r in created} == set(POST_TRADE_ORDER)


async def test_explicit_empty_post_trade_processes_list_seeds_nothing(cfg, session_factory, test_date):
    """An explicitly-uploaded EMPTY post_trade_processes list means ops
    wants none seeded — distinct from the key being absent entirely."""
    workflow_json = build_default_workflow_json([], post_trade_processes=[])
    async with session_factory() as session:
        await repository.upload(session, test_date, workflow_json, uploaded_by="test")
        await session.commit()

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
        created = await repository.seed_post_trade_processes(session, workflow, test_date)
        await session.commit()

    assert created == []
