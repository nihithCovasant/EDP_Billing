"""
Coverage for download_file's connection-retry behavior (src/tools/edpb_download.py).

Retry applies ONLY to httpx.ConnectError (request never reached the server —
safe to retry). httpx.TimeoutException must NEVER be retried, since the
server-side download keeps running after a client timeout and a blind retry
would risk firing a second concurrent download.
"""

from __future__ import annotations

import httpx
import pytest

import src.tools.edpb_download as edpb_download


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = text

    def json(self):
        return self._json_body


class _RaisingThenSucceedingClient:
    """Fake httpx.AsyncClient — raises ConnectError on the first N calls
    (tracked via a shared mutable counter), then succeeds."""
    _fail_count = 0
    _call_log = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        type(self)._call_log.append(url)
        if len(type(self)._call_log) <= type(self)._fail_count:
            raise httpx.ConnectError("connection refused")
        return _FakeResponse(200, {"status": "success", "message": "ok"})


class _AlwaysConnectErrorClient:
    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        raise httpx.ConnectError("connection refused")


class _AlwaysTimeoutClient:
    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        raise httpx.ReadTimeout("timed out")


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Don't actually sleep between retries in tests."""
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr(edpb_download.asyncio, "sleep", no_sleep)


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    monkeypatch.setattr(edpb_download, "_config_value", lambda env, key, default: default)


async def test_connect_error_is_retried_and_eventually_succeeds(monkeypatch):
    _RaisingThenSucceedingClient._fail_count = 2
    _RaisingThenSucceedingClient._call_log = []
    monkeypatch.setattr(edpb_download.httpx, "AsyncClient", _RaisingThenSucceedingClient)

    result = await _invoke(edpb_download.download_file, identifier="EQ", trade_date="2026-07-10")

    assert len(_RaisingThenSucceedingClient._call_log) == 3  # 2 failures + 1 success
    assert "success" in result.lower()


async def test_connect_error_exhausts_retries_and_reports_failure(monkeypatch):
    monkeypatch.setattr(edpb_download.httpx, "AsyncClient", _AlwaysConnectErrorClient)

    result = await _invoke(edpb_download.download_file, identifier="EQ", trade_date="2026-07-10")

    assert "Could not reach" in result
    assert "3 attempts" in result


async def test_timeout_is_never_retried(monkeypatch):
    call_count = {"n": 0}

    class CountingTimeoutClient(_AlwaysTimeoutClient):
        async def post(self, url, json=None, headers=None):
            call_count["n"] += 1
            return await super().post(url, json=json, headers=headers)

    monkeypatch.setattr(edpb_download.httpx, "AsyncClient", CountingTimeoutClient)

    result = await _invoke(edpb_download.download_file, identifier="EQ", trade_date="2026-07-10")

    assert call_count["n"] == 1, "a read-timeout must never be auto-retried (risk of duplicate server-side download)"
    assert "Timed out" in result
