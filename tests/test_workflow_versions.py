"""
Named workflow config versions (edpb_properties.version_name).

A version_name is an independent label that can be attached to any
edpb_properties row, letting ops save a config once and re-apply it later
by name instead of re-pasting the JSON. At most one row owns a given name
at a time (case-insensitive unique partial index in models.py) -- these
tests cover the repository layer (save/list/get/move/clear), the API
endpoints (list/get/apply/delete + upload's version_name/overwrite_version
params), and the extra _validate_workflow_json() checks added alongside it
(HH:MM format, known segment_code, duplicate segment_code).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from datetime import timedelta

import pydantic

from src.agent.edp import repository
import src.agent.edp.api.workflow as workflow_module
from src.agent.edp.api.schemas import WorkflowUploadRequest
from src.agent.edp.api.workflow import router as workflow_router, _validate_workflow_json
from src.agent.edp.config import build_default_workflow_json

from . import helpers


def _version_name() -> str:
    return f"test_version_{uuid.uuid4().hex[:12]}"


def _simple_workflow_json() -> dict:
    segments = [
        {
            "segment_code": "EQ",
            "login_id": "CV0001",
            "window_start": "17:00",
            "window_end": "06:00",
        }
    ]
    return build_default_workflow_json(segments)


# =============================================================================
# Repository layer
# =============================================================================

async def test_upload_with_version_name_attaches_it_to_new_row(cfg, session_factory, test_date):
    name = _version_name()
    async with session_factory() as session:
        row, is_new = await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    assert is_new is True
    assert row.version_name == name

    async with session_factory() as session:
        found = await repository.get_by_version_name(session, name)
    assert found is not None
    assert found.id == row.id


async def test_get_by_version_name_is_case_insensitive(cfg, session_factory, test_date):
    name = _version_name()
    async with session_factory() as session:
        await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with session_factory() as session:
        found = await repository.get_by_version_name(session, name.upper())
    assert found is not None
    assert found.version_name == name


async def test_upload_duplicate_version_name_without_overwrite_raises(cfg, session_factory, test_date):
    name = _version_name()
    async with session_factory() as session:
        await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ValueError):
            await repository.upload(
                session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
            )


async def test_upload_duplicate_version_name_with_overwrite_moves_it(cfg, session_factory, test_date):
    name = _version_name()
    async with session_factory() as session:
        first_row, _ = await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with session_factory() as session:
        second_row, _ = await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test",
            version_name=name, overwrite_version=True,
        )
        await session.commit()

    async with session_factory() as session:
        owner = await repository.get_by_version_name(session, name)
    assert owner is not None
    assert owner.id == second_row.id, "name must move to the new row"

    async with session_factory() as session:
        # first_row was superseded by the second upload, so it's no longer
        # "active" -- fetch it directly by id via get_history() instead.
        history = await repository.get_workflow_history(session, test_date)
        refreshed_first_row = next(r for r in history if r.id == first_row.id)
    assert refreshed_first_row.version_name is None, "name must be cleared off the previous owner"


async def test_list_versions_only_returns_named_rows(cfg, session_factory, test_date):
    name = _version_name()
    async with session_factory() as session:
        # Unnamed upload -- must not show up in list_versions().
        await repository.upload(session, test_date, _simple_workflow_json(), uploaded_by="test")
        await session.commit()

    async with session_factory() as session:
        await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with session_factory() as session:
        versions = await repository.list_versions(session)
    names = {v.version_name for v in versions}
    assert name in names
    assert None not in names


async def test_clear_version_name_detaches_but_keeps_row(cfg, session_factory, test_date):
    name = _version_name()
    async with session_factory() as session:
        row, _ = await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with session_factory() as session:
        removed = await repository.clear_version_name(session, name)
        await session.commit()
    assert removed is True

    async with session_factory() as session:
        assert await repository.get_by_version_name(session, name) is None
        still_active = await repository.get_active(session, test_date)
    assert still_active is not None
    assert still_active.id == row.id, "clearing the name must not touch the underlying config row"


async def test_clear_version_name_returns_false_for_unknown_name(cfg, session_factory, test_date):
    async with session_factory() as session:
        removed = await repository.clear_version_name(session, _version_name())
    assert removed is False


async def test_bootstrap_style_upload_keeps_default_name_moving_forward(cfg, session_factory, test_date):
    """
    Mirrors orchestrator.run_wake_cycle()'s auto-seed call
    (version_name="default", overwrite_version=True) across two different
    trade_dates -- "default" must always end up on the most recently
    auto-seeded row, with no ValueError on the 2nd+ occurrence and no
    dangling copy left on the 1st row.
    """
    next_day = test_date + timedelta(days=1)
    await helpers.cleanup_day(session_factory, next_day)
    try:
        async with session_factory() as session:
            first_row, _ = await repository.upload(
                session, test_date, _simple_workflow_json(), uploaded_by="agent-bootstrap",
                version_name="default", overwrite_version=True,
            )
            await session.commit()
        assert first_row.version_name == "default"

        async with session_factory() as session:
            second_row, _ = await repository.upload(
                session, next_day, _simple_workflow_json(), uploaded_by="agent-bootstrap",
                version_name="default", overwrite_version=True,
            )
            await session.commit()

        async with session_factory() as session:
            owner = await repository.get_by_version_name(session, "default")
            history = await repository.get_workflow_history(session, test_date)
        assert owner is not None
        assert owner.id == second_row.id
        refreshed_first_row = next(r for r in history if r.id == first_row.id)
        assert refreshed_first_row.version_name is None
    finally:
        await helpers.cleanup_day(session_factory, next_day)


# =============================================================================
# API layer -- version_name is a required field on WorkflowUploadRequest
# =============================================================================

def test_workflow_upload_request_requires_version_name():
    with pytest.raises(pydantic.ValidationError):
        WorkflowUploadRequest(workflow_json=_simple_workflow_json(), uploaded_by="ops")


def test_workflow_upload_request_rejects_blank_version_name():
    with pytest.raises(pydantic.ValidationError):
        WorkflowUploadRequest(workflow_json=_simple_workflow_json(), uploaded_by="ops", version_name="   ")


def test_workflow_upload_request_accepts_valid_version_name():
    req = WorkflowUploadRequest(
        workflow_json=_simple_workflow_json(), uploaded_by="ops", version_name="my_config",
    )
    assert req.version_name == "my_config"


# =============================================================================
# API layer -- validation
# =============================================================================

def test_validate_workflow_json_rejects_malformed_window_time():
    bad = build_default_workflow_json([
        {"segment_code": "EQ", "login_id": "CV0001", "window_start": "5pm", "window_end": "06:00"},
    ])
    with pytest.raises(Exception) as exc_info:
        _validate_workflow_json(bad)
    assert "window_start" in str(exc_info.value)


def test_validate_workflow_json_rejects_out_of_range_time():
    bad = build_default_workflow_json([
        {"segment_code": "EQ", "login_id": "CV0001", "window_start": "25:00", "window_end": "06:00"},
    ])
    with pytest.raises(Exception):
        _validate_workflow_json(bad)


def test_validate_workflow_json_rejects_unknown_segment_code():
    bad = build_default_workflow_json([
        {"segment_code": "NOT_A_REAL_SEGMENT", "login_id": "CV0001", "window_start": "17:00", "window_end": "06:00"},
    ])
    with pytest.raises(Exception) as exc_info:
        _validate_workflow_json(bad)
    assert "unknown segment_code" in str(exc_info.value)


def test_validate_workflow_json_rejects_duplicate_segment_code():
    bad = build_default_workflow_json([
        {"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:00", "window_end": "06:00"},
        {"segment_code": "EQ", "login_id": "CV0002", "window_start": "17:00", "window_end": "06:00"},
    ])
    with pytest.raises(Exception) as exc_info:
        _validate_workflow_json(bad)
    assert "duplicate segment_code" in str(exc_info.value)


def test_validate_workflow_json_accepts_valid_config():
    good = _simple_workflow_json()
    _validate_workflow_json(good)  # must not raise


# =============================================================================
# API layer -- version endpoints (list / get / apply / delete)
# =============================================================================

ADMIN_HEADERS = {"X-User-Role": "System Administrator"}


@pytest.fixture
def api_client():
    app = FastAPI()
    app.include_router(workflow_router, prefix="/edp")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_list_and_get_version_endpoints(cfg, session_factory, test_date, api_client):
    name = _version_name()

    # Exercise the list/get endpoints directly against a row seeded via the
    # repository -- the upload endpoint always resolves real "today" (see
    # resolve_active_date() in upload_workflow()), not this test's isolated
    # far-future test_date, so it's not useful to drive from here.
    async with session_factory() as session:
        await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with api_client as client:
        list_resp = await client.get("/edp/workflow/versions")
        assert list_resp.status_code == 200
        assert any(v["version_name"] == name for v in list_resp.json())

        get_resp = await client.get(f"/edp/workflow/versions/{name}")
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["version_name"] == name
        assert "workflow_json" in body

        missing_resp = await client.get(f"/edp/workflow/versions/{_version_name()}")
        assert missing_resp.status_code == 404


async def test_apply_workflow_version_is_noop_when_already_active(
    cfg, session_factory, test_date, api_client, monkeypatch,
):
    """
    Applying a version that's already today's active config must NOT
    create a duplicate row — see the CASE reported by the user: "get all
    workflow versions" -> "set default as active" was creating a brand
    new "ops"-uploaded row instead of recognizing "default" was already
    active.
    """
    name = _version_name()
    monkeypatch.setattr(workflow_module, "resolve_active_date", lambda *a, **k: test_date)

    async with session_factory() as session:
        row, _ = await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with api_client as client:
        resp = await client.post(f"/edp/workflow/versions/{name}/apply", json={}, headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_new"] is False
        assert body["id"] == row.id
        assert body["version_name"] == name

    # No second row must have been created for this trade_date.
    async with session_factory() as session:
        history = await repository.get_workflow_history(session, test_date)
    assert len(history) == 1


async def test_apply_workflow_version_creates_new_row_and_moves_name_when_not_active(
    cfg, session_factory, test_date, api_client, monkeypatch,
):
    name = _version_name()
    monkeypatch.setattr(workflow_module, "resolve_active_date", lambda *a, **k: test_date)

    async with session_factory() as session:
        saved_row, _ = await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with session_factory() as session:
        # Something else becomes today's active config -- "name" is now saved but not active.
        await repository.upload(session, test_date, _simple_workflow_json(), uploaded_by="someone-else")
        await session.commit()

    async with api_client as client:
        resp = await client.post(f"/edp/workflow/versions/{name}/apply", json={}, headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_new"] is True
        assert body["id"] != saved_row.id, "must create a fresh row, not reuse the saved one directly"
        assert body["version_name"] == name, "name must move onto the newly-applied row"

    async with session_factory() as session:
        active = await repository.get_active(session, test_date)
        assert active.id == body["id"]
        assert active.version_name == name


async def test_delete_workflow_version_endpoint(cfg, session_factory, test_date, api_client):
    name = _version_name()
    async with session_factory() as session:
        await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with api_client as client:
        resp = await client.delete(f"/edp/workflow/versions/{name}", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        second = await client.delete(f"/edp/workflow/versions/{name}", headers=ADMIN_HEADERS)
        assert second.status_code == 404


# =============================================================================
# API layer -- mutating endpoints require the System Administrator role
# =============================================================================

async def test_apply_workflow_version_rejects_non_admin_role(cfg, session_factory, test_date, api_client):
    name = _version_name()
    async with session_factory() as session:
        await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with api_client as client:
        no_role_resp = await client.post(f"/edp/workflow/versions/{name}/apply", json={})
        assert no_role_resp.status_code == 403

        wrong_role_resp = await client.post(
            f"/edp/workflow/versions/{name}/apply", json={}, headers={"X-User-Role": "Viewer"},
        )
        assert wrong_role_resp.status_code == 403


async def test_delete_workflow_version_rejects_non_admin_role(cfg, session_factory, test_date, api_client):
    name = _version_name()
    async with session_factory() as session:
        await repository.upload(
            session, test_date, _simple_workflow_json(), uploaded_by="test", version_name=name,
        )
        await session.commit()

    async with api_client as client:
        resp = await client.delete(f"/edp/workflow/versions/{name}")
        assert resp.status_code == 403

    # The version must still exist -- the rejected request must not have
    # touched anything.
    async with session_factory() as session:
        assert await repository.get_by_version_name(session, name) is not None


async def test_upload_workflow_rejects_non_admin_role(api_client):
    async with api_client as client:
        resp = await client.post(
            "/edp/workflow/upload",
            json={"workflow_json": _simple_workflow_json(), "version_name": _version_name()},
        )
    assert resp.status_code == 403
