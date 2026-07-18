"""
edpb_audit_log — config-change audit trail.

Scope: workflow uploads (incl. the "quick patch" update_edp_segment_window
chat tool, which re-uploads under the hood — see api/workflow.py's shared
_upload_workflow_for_date()) and named-version deletes. See models.py::
AuditLog and api/workflow.py::diff_workflow_configs()/_resolve_actor() for
the full rationale.
"""

from __future__ import annotations

import uuid

from src.agent.edp import repository
from src.agent.edp.api.workflow import (
    _upload_workflow_for_date,
    delete_workflow_version,
    diff_workflow_configs,
)
from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.models import AuditAction

from . import helpers


def _version_name() -> str:
    return f"audit_test_{uuid.uuid4().hex[:12]}"


def _workflow_json(window_start="17:00", window_end="18:00") -> dict:
    segments = [
        {
            "segment_code": "EQ",
            "login_id": "CV0001",
            "window_start": window_start,
            "window_end": window_end,
        }
    ]
    return build_default_workflow_json(segments)


# =============================================================================
# diff_workflow_configs()
# =============================================================================

def test_diff_reports_initial_config_for_first_upload():
    summary, changes = diff_workflow_configs(None, _workflow_json())
    assert "Initial" in summary
    assert changes["initial"] is True


def test_diff_detects_window_field_change():
    old = _workflow_json(window_start="17:00")
    new = _workflow_json(window_start="18:00")
    summary, changes = diff_workflow_configs(old, new)
    assert "EQ" in summary and "17:00" in summary and "18:00" in summary
    modified = [c for c in changes["segments"] if c["change"] == "modified"]
    assert modified == [{"code": "EQ", "change": "modified", "field": "window_start", "old": "17:00", "new": "18:00"}]


def test_diff_reports_no_changes_for_identical_reupload():
    wf = _workflow_json()
    summary, changes = diff_workflow_configs(wf, dict(wf))
    assert "no effective changes" in summary
    assert changes["segments"] == []


def test_diff_detects_added_and_removed_segments():
    old = build_default_workflow_json([
        {"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"},
    ])
    new = build_default_workflow_json([
        {"segment_code": "DR", "login_id": "CV0001", "window_start": "18:00", "window_end": "21:00"},
    ])
    summary, changes = diff_workflow_configs(old, new)
    added = [c["code"] for c in changes["segments"] if c["change"] == "added"]
    removed = [c["code"] for c in changes["segments"] if c["change"] == "removed"]
    assert added == ["DR"]
    assert removed == ["EQ"]
    assert "added DR" in summary and "removed EQ" in summary


# =============================================================================
# Upload -> audit row wiring
# =============================================================================

async def test_first_upload_for_a_date_records_one_audit_entry(cfg, session_factory, test_date):
    """
    NOTE: doesn't assert the summary says "Initial" -- get_latest_effective()
    may legitimately carry forward some other far-future test's leftover
    config as the "prior" one to diff against (same carry-forward behavior
    production relies on), so this only checks that exactly one audit row
    was recorded, correctly tagged. diff_workflow_configs()'s own "no prior
    config at all" case is covered directly by
    test_diff_reports_initial_config_for_first_upload above.
    """
    await _upload_workflow_for_date(test_date, _workflow_json(), "ops")

    async with session_factory() as session:
        history = await repository.get_audit_history(session, trade_date=test_date)

    assert len(history) == 1
    entry = history[0]
    assert entry.action == AuditAction.WORKFLOW_UPLOAD
    assert entry.trade_date == test_date
    assert entry.summary  # non-empty, some description of the change
    assert entry.actor  # some actor string, even if just the fallback


async def test_reupload_with_changed_window_records_diff(cfg, session_factory, test_date):
    await _upload_workflow_for_date(test_date, _workflow_json(window_start="17:00"), "ops")
    await _upload_workflow_for_date(test_date, _workflow_json(window_start="19:00"), "ops")

    async with session_factory() as session:
        history = await repository.get_audit_history(session, trade_date=test_date)

    assert len(history) == 2
    latest = history[0]  # most recent first
    assert latest.action == AuditAction.WORKFLOW_UPLOAD
    assert "17:00" in latest.summary and "19:00" in latest.summary
    assert latest.changes_json["segments"][0]["field"] == "window_start"


async def test_uploaded_by_fallback_is_used_when_no_request_context(cfg, session_factory, test_date):
    """Outside a real HTTP request there's no X-User-ID in flight, so the
    actor recorded on the audit row must fall back to the explicit
    uploaded_by the caller passed in (get_request_context() returns None
    for a bare function call / background task, same as real usage from
    the orchestrator's own bootstrap upload)."""
    await _upload_workflow_for_date(test_date, _workflow_json(), "agent-bootstrap")

    async with session_factory() as session:
        history = await repository.get_audit_history(session, trade_date=test_date)

    assert history[0].actor == "agent-bootstrap"


async def test_version_name_and_config_id_are_captured_on_upload(cfg, session_factory, test_date):
    resp = await _upload_workflow_for_date(test_date, _workflow_json(), "ops", version_name="my_ver")

    async with session_factory() as session:
        history = await repository.get_audit_history(session, trade_date=test_date)

    assert history[0].version_name == "my_ver"
    assert history[0].config_id == resp["id"]


# =============================================================================
# Delete-version -> audit row wiring
# =============================================================================

async def test_deleting_a_version_records_audit_entry(cfg, session_factory, test_date):
    name = _version_name()
    await _upload_workflow_for_date(test_date, _workflow_json(), "ops", version_name=name)

    await delete_workflow_version(name)

    async with session_factory() as session:
        history = await repository.get_audit_history(session, action="WORKFLOW_VERSION_DELETE", limit=200)

    matching = [h for h in history if h.version_name == name]
    assert len(matching) == 1
    assert matching[0].action == AuditAction.WORKFLOW_VERSION_DELETE


async def test_deleting_unknown_version_records_no_audit_entry(cfg, session_factory, test_date):
    from fastapi import HTTPException
    import pytest

    async with session_factory() as session:
        before = await repository.get_audit_history(session, action="WORKFLOW_VERSION_DELETE")

    with pytest.raises(HTTPException):
        await delete_workflow_version("definitely_not_a_real_version_name")

    async with session_factory() as session:
        after = await repository.get_audit_history(session, action="WORKFLOW_VERSION_DELETE")

    assert len(after) == len(before)


# =============================================================================
# Repository layer
# =============================================================================

async def test_get_audit_history_filters_by_trade_date(cfg, session_factory, test_date):
    other_date = test_date.replace(year=test_date.year + 1)
    await helpers.cleanup_day(session_factory, other_date)
    try:
        await _upload_workflow_for_date(test_date, _workflow_json(), "ops")
        await _upload_workflow_for_date(other_date, _workflow_json(), "ops")

        async with session_factory() as session:
            history = await repository.get_audit_history(session, trade_date=test_date)

        assert len(history) == 1
        assert history[0].trade_date == test_date
    finally:
        await helpers.cleanup_day(session_factory, other_date)


async def test_get_audit_history_respects_limit(cfg, session_factory, test_date):
    for i in range(3):
        await _upload_workflow_for_date(test_date, _workflow_json(window_start=f"1{i}:00"), "ops")

    async with session_factory() as session:
        history = await repository.get_audit_history(session, trade_date=test_date, limit=2)

    assert len(history) == 2
