"""
Chat-tool-level behavior for EDP workflow config version naming
(src/tools/edp_status.py).

These tools are plain HTTP clients against this agent's own /edp/* API
(see module docstring in edp_status.py) -- tests here monkeypatch the
module's own _get()/_post() helpers so no real HTTP call or database is
involved; this file only verifies the tools build the right request
payloads and produce the right chat-facing text for each response shape.
"""

from __future__ import annotations

import src.tools.edp_status as edp_status


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


# =============================================================================
# _actor_headers() -- forwards the current request's caller identity/role
# onto this agent's own internal /edp/* API calls (see api/auth.py's
# require_admin_role, which needs X-User-Role to recognize a chat-driven
# config change as coming from an admin).
# =============================================================================


class _FakeRequestContext:
    def __init__(self, userid):
        self.userid = userid


def test_actor_headers_forwards_userid_and_role(monkeypatch):
    monkeypatch.setattr(edp_status, "get_request_context", lambda: _FakeRequestContext("a@b.com (uid:1)"))
    monkeypatch.setattr(edp_status, "get_current_role", lambda: "System Administrator")

    headers = edp_status._actor_headers()

    assert headers == {"X-User-ID": "a@b.com (uid:1)", "X-User-Role": "System Administrator"}


def test_actor_headers_omits_missing_values(monkeypatch):
    monkeypatch.setattr(edp_status, "get_request_context", lambda: None)
    monkeypatch.setattr(edp_status, "get_current_role", lambda: None)

    assert edp_status._actor_headers() == {}


def test_actor_headers_ignores_placeholder_na_userid(monkeypatch):
    monkeypatch.setattr(edp_status, "get_request_context", lambda: _FakeRequestContext("N/A"))
    monkeypatch.setattr(edp_status, "get_current_role", lambda: None)

    assert edp_status._actor_headers() == {}


# =============================================================================
# update_edp_segment_window -- version_name is a required argument, and
# whether overwrite_version ends up True depends on whether the caller's
# chosen name matches the config's current name.
# =============================================================================


async def test_update_segment_window_requires_version_name_argument():
    """version_name has no default -- omitting it must be a tool-invocation
    error (StructuredTool argument validation), not a silent guess."""
    import pydantic

    try:
        await _invoke(edp_status.update_edp_segment_window, identifier="EQ", window_start="17:00")
    except pydantic.ValidationError:
        return
    except Exception as exc:  # some langchain versions raise a different wrapper
        assert "version_name" in str(exc)
        return
    raise AssertionError("expected a validation error for missing version_name")


async def test_update_segment_window_reusing_current_name_auto_overwrites(monkeypatch):
    captured = {}

    async def fake_get(path):
        return 200, {
            "trade_date": "2026-07-14",
            "carried_forward": False,
            "version_name": "default",
            "workflow_json": {
                "segments": [
                    {"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"},
                ],
            },
        }

    async def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return 200, {
            "trade_date": "2026-07-14",
            "resolved_trade_date": "2026-07-14",
            "deferred": False,
            "version_name": body["version_name"],
        }

    monkeypatch.setattr(edp_status, "_get", fake_get)
    monkeypatch.setattr(edp_status, "_post", fake_post)

    result = await _invoke(
        edp_status.update_edp_segment_window,
        identifier="EQ",
        version_name="default",
        window_start="5 PM",
    )

    assert captured["body"]["version_name"] == "default"
    assert captured["body"]["overwrite_version"] is True, (
        "re-using the config's own current name is a continuation, not a fork -- "
        "must overwrite in place without asking for a different name"
    )
    assert "default" in result


async def test_update_segment_window_new_name_does_not_force_overwrite(monkeypatch):
    captured = {}

    async def fake_get(path):
        return 200, {
            "trade_date": "2026-07-14",
            "carried_forward": False,
            "version_name": "default",
            "workflow_json": {
                "segments": [
                    {"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"},
                ],
            },
        }

    async def fake_post(path, body):
        captured["body"] = body
        return 200, {
            "trade_date": "2026-07-14",
            "resolved_trade_date": "2026-07-14",
            "deferred": False,
            "version_name": body["version_name"],
        }

    monkeypatch.setattr(edp_status, "_get", fake_get)
    monkeypatch.setattr(edp_status, "_post", fake_post)

    await _invoke(
        edp_status.update_edp_segment_window,
        identifier="EQ",
        version_name="brand_new_name",
        window_start="5 PM",
    )

    assert captured["body"]["version_name"] == "brand_new_name"
    assert captured["body"]["overwrite_version"] is False, (
        "a genuinely new name must not silently overwrite whatever else might "
        "already own it -- let the 409 check decide"
    )


async def test_update_segment_window_surfaces_409_as_friendly_message(monkeypatch):
    async def fake_get(path):
        return 200, {
            "trade_date": "2026-07-14",
            "carried_forward": False,
            "version_name": "default",
            "workflow_json": {
                "segments": [
                    {"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"},
                ],
            },
        }

    async def fake_post(path, body):
        return 409, {"detail": "version_name 'taken' already exists"}

    monkeypatch.setattr(edp_status, "_get", fake_get)
    monkeypatch.setattr(edp_status, "_post", fake_post)

    result = await _invoke(
        edp_status.update_edp_segment_window,
        identifier="EQ",
        version_name="taken",
        window_start="5 PM",
    )
    assert "already exists" in result
    assert "❌" in result


# =============================================================================
# upload_edp_workflow_config -- version_name required, 409 surfaced nicely.
# =============================================================================


async def test_upload_workflow_config_requires_version_name_argument():
    import pydantic

    try:
        await _invoke(edp_status.upload_edp_workflow_config, workflow_json={"segments": []})
    except pydantic.ValidationError:
        return
    except Exception as exc:
        assert "version_name" in str(exc)
        return
    raise AssertionError("expected a validation error for missing version_name")


async def test_upload_workflow_config_passes_version_name_and_overwrite_through(monkeypatch):
    captured = {}

    async def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return 200, {
            "trade_date": "2026-07-14",
            "resolved_trade_date": "2026-07-14",
            "deferred": False,
            "segment_count": 1,
            "post_trade_process_count": None,
            "uploaded_by": "chat-user",
            "id": "abc123",
            "version_name": body["version_name"],
        }

    monkeypatch.setattr(edp_status, "_post", fake_post)

    result = await _invoke(
        edp_status.upload_edp_workflow_config,
        workflow_json={
            "segments": [{"segment_code": "EQ", "login_id": "CV0001", "window_start": "17:00", "window_end": "18:00"}]
        },
        version_name="my_new_config",
        overwrite_version=True,
    )

    assert captured["path"] == "/edp/workflow/upload"
    assert captured["body"]["version_name"] == "my_new_config"
    assert captured["body"]["overwrite_version"] is True
    assert "my_new_config" in result


async def test_upload_workflow_config_surfaces_409_as_friendly_message(monkeypatch):
    async def fake_post(path, body):
        return 409, {"detail": "version_name 'taken' already exists"}

    monkeypatch.setattr(edp_status, "_post", fake_post)

    result = await _invoke(
        edp_status.upload_edp_workflow_config,
        workflow_json={"segments": []},
        version_name="taken",
    )
    assert "already exists" in result
    assert "❌" in result
