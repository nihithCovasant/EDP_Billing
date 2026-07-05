"""
Mid-day config-change protection.

Ops does NOT upload a config every day — only when something changes. Since
window_start/window_end/login_id are resolved LIVE from the active config
on every wake cycle (see orchestrator._resolve_window()), an upload that
lands while today's trade_date is already mid-run would otherwise change
those values for segments still in flight. repository.has_processing_started()
+ the /workflow/upload endpoint's defer logic (see api/workflow.py) protect
against that: any upload targeting a trade_date where at least one segment
has left PENDING is silently redirected to trade_date + 1 instead of
mutating today's config, and the response reports `deferred=True` plus the
`requested_trade_date` that was asked for vs. `trade_date` where it actually
landed.

A trade_date with every segment still PENDING (nothing seeded, or windows
simply haven't opened yet) is NOT "started" — same-day changes must still
apply immediately in that case, since nothing has actually run yet.
"""

from __future__ import annotations

from datetime import timedelta

from src.agent.edp import repository
from src.agent.edp.api.workflow import upload_workflow
from src.agent.edp.api.schemas import WorkflowUploadRequest
from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.models import SegmentStatus

from . import helpers


def _workflow_json(cfg, login_id: str = "CV0001") -> dict:
    segments = [
        {
            "segment_code": code,
            "login_id": login_id,
            "window_start": "00:00",
            "window_end": "23:59",
            "window_end_next_day": True,
        }
        for code in helpers.SEGMENT_ORDER
    ]
    return build_default_workflow_json(segments, timezone=cfg.timezone)


async def test_upload_applies_immediately_when_nothing_started(cfg, session_factory, test_date):
    """All rows PENDING (or nothing seeded at all) — upload takes effect for the requested date itself."""
    await helpers.seed_day(session_factory, test_date, cfg)

    resp = await upload_workflow(WorkflowUploadRequest(
        trade_date=test_date,
        workflow_json=_workflow_json(cfg, "CHANGED"),
        uploaded_by="ops",
    ))

    assert resp["deferred"] is False
    assert resp["trade_date"] == test_date
    assert resp["requested_trade_date"] == test_date

    async with session_factory() as session:
        active = await repository.get_active(session, test_date)
    assert active is not None
    assert active.workflow_json["segments"][0]["login_id"] == "CHANGED"


async def test_upload_deferred_to_next_day_once_a_segment_has_started(cfg, session_factory, test_date):
    """One segment already IN_PROGRESS — upload must NOT touch today; it lands on test_date + 1 instead."""
    next_day = test_date + timedelta(days=1)
    await helpers.cleanup_day(session_factory, next_day)
    try:
        await helpers.seed_day(session_factory, test_date, cfg)

        # Simulate the first segment having already started this trading day.
        async with session_factory() as session:
            row = await repository.get_one(session, test_date, helpers.SEGMENT_ORDER[0])
            row.segment_status = SegmentStatus.IN_PROGRESS
            await session.commit()

        async with session_factory() as session:
            original_active = await repository.get_active(session, test_date)
        original_hash = original_active.content_hash

        resp = await upload_workflow(WorkflowUploadRequest(
            trade_date=test_date,
            workflow_json=_workflow_json(cfg, "SHOULD_NOT_APPLY_TODAY"),
            uploaded_by="ops",
        ))

        assert resp["deferred"] is True
        assert resp["requested_trade_date"] == test_date
        assert resp["trade_date"] == next_day, "deferred upload must land on trade_date + 1"

        # Today's active config is completely untouched.
        async with session_factory() as session:
            still_active_today = await repository.get_active(session, test_date)
        assert still_active_today.content_hash == original_hash
        assert still_active_today.workflow_json["segments"][0]["login_id"] != "SHOULD_NOT_APPLY_TODAY"

        # The new config is waiting, active, for tomorrow.
        async with session_factory() as session:
            active_next_day = await repository.get_active(session, next_day)
        assert active_next_day is not None
        assert active_next_day.workflow_json["segments"][0]["login_id"] == "SHOULD_NOT_APPLY_TODAY"
    finally:
        await helpers.cleanup_day(session_factory, next_day)


async def test_upload_deferred_when_only_post_trade_process_has_started(cfg, session_factory, test_date):
    """A started post-trade process row also counts as "processing underway" for that trade_date."""
    next_day = test_date + timedelta(days=1)
    await helpers.cleanup_day(session_factory, next_day)
    try:
        await helpers.seed_day(session_factory, test_date, cfg)
        await helpers.seed_post_trade_day(session_factory, test_date)

        async with session_factory() as session:
            row = await repository.get_one(session, test_date, helpers.ALL_POST_TRADE_CODES[0])
            row.segment_status = SegmentStatus.IN_PROGRESS
            await session.commit()

        resp = await upload_workflow(WorkflowUploadRequest(
            trade_date=test_date,
            workflow_json=_workflow_json(cfg, "SHOULD_DEFER"),
            uploaded_by="ops",
        ))

        assert resp["deferred"] is True
        assert resp["trade_date"] == next_day
    finally:
        await helpers.cleanup_day(session_factory, next_day)
