"""
Chat-tool-level behavior for src/tools/edp_control.py (skip_edp_segment_today,
retry_edp_segment, control_edp_agent) -- same monkeypatch convention as
test_edp_chat_tools.py.
"""

from __future__ import annotations

import src.tools.edp_control as edp_control


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


# =============================================================================
# skip_edp_segment_today
# =============================================================================

async def test_skip_segment_posts_reason_and_resolves_alias(monkeypatch):
    captured = {}

    async def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return 200, {"segment_status": "SKIPPED", "skip_reason": "no trades today", "skip_category": "ops"}

    monkeypatch.setattr(edp_control, "_post", fake_post)
    monkeypatch.setattr(edp_control, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(edp_control.skip_edp_segment_today, identifier="CASH", reason="no trades today")

    assert captured["path"] == "/edp/status/2026-07-10/EQ/skip"
    assert captured["body"] == {"reason": "no trades today", "skipped_by": "chat-user"}
    assert "SKIPPED" in result


async def test_skip_segment_404_when_no_record(monkeypatch):
    async def fake_post(path, body):
        return 404, {"detail": "not found"}

    monkeypatch.setattr(edp_control, "_post", fake_post)
    monkeypatch.setattr(edp_control, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(edp_control.skip_edp_segment_today, identifier="EQ", reason="x")

    assert "No record found" in result


async def test_skip_segment_409_when_already_terminal(monkeypatch):
    async def fake_post(path, body):
        return 409, {"detail": "already terminal"}

    monkeypatch.setattr(edp_control, "_post", fake_post)
    monkeypatch.setattr(edp_control, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(edp_control.skip_edp_segment_today, identifier="EQ", reason="x")

    assert "terminal state" in result


# =============================================================================
# retry_edp_segment
# =============================================================================

async def test_retry_segment_success(monkeypatch):
    captured = {}

    async def fake_post(path, body):
        captured["path"] = path
        return 200, {"segment_status": "PENDING"}

    monkeypatch.setattr(edp_control, "_post", fake_post)
    monkeypatch.setattr(edp_control, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(edp_control.retry_edp_segment, identifier="DR")

    assert captured["path"] == "/edp/status/2026-07-10/DR/retry"
    assert "reset to PENDING" in result


async def test_retry_segment_409_when_not_failed_or_skipped(monkeypatch):
    async def fake_post(path, body):
        return 409, {"detail": "not failed"}

    monkeypatch.setattr(edp_control, "_post", fake_post)
    monkeypatch.setattr(edp_control, "_today_ist", lambda: "2026-07-10")

    result = await _invoke(edp_control.retry_edp_segment, identifier="DR")

    assert "isn't currently FAILED or SKIPPED" in result


# =============================================================================
# control_edp_agent
# =============================================================================

async def test_control_agent_rejects_unknown_action():
    result = await _invoke(edp_control.control_edp_agent, action="pause")
    assert "Unrecognized action" in result


async def test_control_agent_status_renders_history(monkeypatch):
    async def fake_get(path):
        assert path == "/edp/agent/status"
        return 200, {
            "effective_state": "RUNNING",
            "history": [
                {"requested_at": "2026-07-01T10:00:00", "action": "START", "effective_state": "RUNNING",
                 "requested_by": "ops", "reason": "resume"},
            ],
        }

    monkeypatch.setattr(edp_control, "_get", fake_get)

    result = await _invoke(edp_control.control_edp_agent, action="status")

    assert "RUNNING" in result
    assert "resume" in result


async def test_control_agent_stop_includes_snapshot(monkeypatch):
    async def fake_post(path, body):
        assert path == "/edp/agent/stop"
        assert body == {"requested_by": "chat-user", "reason": "Diwali holiday"}
        return 200, {
            "requested_by": "chat-user", "reason": "Diwali holiday",
            "snapshot": {"active_segment": "EQ", "active_process": "BeginFileUpload", "active_state": "INIT"},
        }

    monkeypatch.setattr(edp_control, "_post", fake_post)

    result = await _invoke(edp_control.control_edp_agent, action="stop", reason="Diwali holiday")

    assert "STOPPED" in result
    assert "EQ" in result


async def test_control_agent_start(monkeypatch):
    async def fake_post(path, body):
        assert path == "/edp/agent/start"
        return 200, {"requested_by": "chat-user", "reason": "resume after maintenance"}

    monkeypatch.setattr(edp_control, "_post", fake_post)

    result = await _invoke(edp_control.control_edp_agent, action="START", reason="resume after maintenance")

    assert "STARTED" in result
