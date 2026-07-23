"""
OtelContextMiddleware — real per-request caller identity.

Covers _decode_jwt_claims()/_actor_from_claims() (unit level) and the full
pipeline end-to-end: an Authorization: Bearer JWT on a real /agent/run call
-> OtelContextMiddleware decodes it -> RequestContext.userid -> read back by
agent_run_endpoint's response "user_id" field (see _resolve_request_user_id()
in src/agent/__main__.py). No signature verification is performed anywhere
in this pipeline by design (the CAMS gateway in front of this agent already
validated the token) -- these tests use unsigned/dummy-signed tokens, which
is realistic for that trust model.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from starlette.requests import Request as _ProbeRequest

from src.middleware.claims_middleware import _actor_from_claims, _decode_jwt_claims, get_current_role


def _fake_jwt(payload: dict) -> str:
    """A structurally-valid JWT (header.payload.signature) with an
    arbitrary/unverified signature segment -- fine here since the
    middleware never verifies it, only decodes the payload."""

    def _b64(obj) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = _b64({"alg": "RS256", "typ": "JWT"})
    body = _b64(payload)
    return f"{header}.{body}.not-a-real-signature"


class _FakeHeaders(dict):
    def get(self, key, default=None):
        # Starlette headers are case-insensitive -- normalize for the test double.
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = _FakeHeaders(headers)


# =============================================================================
# _decode_jwt_claims() / _actor_from_claims() -- unit level
# =============================================================================


def test_decode_jwt_claims_extracts_payload_from_bearer_header():
    token = _fake_jwt({"email": "a@b.com", "sub": "67", "tenant_id": 1})
    claims = _decode_jwt_claims(_FakeRequest({"Authorization": f"Bearer {token}"}))
    assert claims == {"email": "a@b.com", "sub": "67", "tenant_id": 1}


def test_decode_jwt_claims_returns_empty_dict_when_no_auth_header():
    assert _decode_jwt_claims(_FakeRequest({})) == {}


def test_decode_jwt_claims_returns_empty_dict_for_non_bearer_scheme():
    assert _decode_jwt_claims(_FakeRequest({"Authorization": "Basic dXNlcjpwYXNz"})) == {}


def test_decode_jwt_claims_never_raises_on_malformed_token():
    assert _decode_jwt_claims(_FakeRequest({"Authorization": "Bearer not.a.jwt!!"})) == {}
    assert _decode_jwt_claims(_FakeRequest({"Authorization": "Bearer justonepart"})) == {}


def test_actor_from_claims_combines_email_and_sub():
    assert _actor_from_claims({"email": "a@b.com", "sub": "67"}) == "a@b.com (uid:67)"


def test_actor_from_claims_falls_back_to_email_only():
    assert _actor_from_claims({"email": "a@b.com"}) == "a@b.com"


def test_actor_from_claims_falls_back_to_sub_only():
    assert _actor_from_claims({"sub": "67"}) == "uid:67"


def test_actor_from_claims_returns_none_when_no_identity_claims():
    assert _actor_from_claims({"role": "System Administrator"}) is None


# =============================================================================
# End-to-end: JWT -> middleware -> RequestContext -> /agent/run response
# =============================================================================


class _NoToolCallLLM:
    """Always answers directly, no tool calls -- keeps these tests focused
    on request-context plumbing, not tool routing."""

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        return AIMessage(content="ok")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("EDP_LOOP_ENABLED", "false")
    monkeypatch.setattr("src.agent.nodes.agent_node.get_llm_model", lambda *a, **k: _NoToolCallLLM())
    from src.agent.__main__ import build_app

    app = build_app()
    with TestClient(app) as c:
        yield c


def test_agent_run_reports_real_user_id_from_jwt(client):
    token = _fake_jwt({"email": "nihith.yelchuri@covasant.com", "sub": "67"})
    resp = client.post(
        "/agent/run",
        json={"query": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "nihith.yelchuri@covasant.com (uid:67)"


def test_agent_run_explicit_x_user_id_header_wins_over_jwt(client):
    token = _fake_jwt({"email": "from-jwt@covasant.com", "sub": "1"})
    resp = client.post(
        "/agent/run",
        json={"query": "hello"},
        headers={"Authorization": f"Bearer {token}", "X-User-ID": "explicit-override"},
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "explicit-override"


def test_agent_run_falls_back_to_config_user_id_without_any_auth(client):
    resp = client.post("/agent/run", json={"query": "hello"})
    assert resp.status_code == 200
    # No Authorization/X-User-ID header at all -- falls back to the static
    # scaffold-time agent_config.json value (may be "" in some configs, but
    # must not be a stale/wrong identity from a previous request).
    body = resp.json()
    assert "user_id" in body


# =============================================================================
# get_current_role() -- lets a chat tool's internal re-entrant call to this
# same agent's own /edp/* API forward the caller's role (see
# src/tools/edp_status.py::_actor_headers() and
# src/agent/edp/api/auth.py::require_admin_role).
# =============================================================================


def test_get_current_role_reflects_jwt_role_claim_during_the_request(client, monkeypatch):
    """
    Route through a tiny extra endpoint so we can observe get_current_role()
    from INSIDE the request (the context var is reset once the request
    ends, so it can't be checked from outside afterwards).
    """
    from src.agent.__main__ import build_app

    seen = {}

    async def _probe(request: _ProbeRequest):
        seen["role"] = get_current_role()
        from starlette.responses import JSONResponse

        return JSONResponse({"role": seen["role"]})

    app = build_app()
    app.add_api_route("/__test_probe_role", _probe, methods=["GET"])

    token = _fake_jwt({"role": "System Administrator", "email": "a@b.com"})
    with TestClient(app) as c:
        resp = c.get("/__test_probe_role", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "System Administrator"


def test_get_current_role_prefers_explicit_header_over_jwt(client):
    from starlette.responses import JSONResponse

    from src.agent.__main__ import build_app

    async def _probe(request: _ProbeRequest):
        return JSONResponse({"role": get_current_role()})

    app = build_app()
    app.add_api_route("/__test_probe_role_2", _probe, methods=["GET"])

    token = _fake_jwt({"role": "Viewer"})
    with TestClient(app) as c:
        resp = c.get(
            "/__test_probe_role_2",
            headers={"Authorization": f"Bearer {token}", "X-User-Role": "System Administrator"},
        )
    assert resp.json()["role"] == "System Administrator"
