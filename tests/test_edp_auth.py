"""
require_admin_role() -- role-based access control for mutating EDP workflow
endpoints (see src/agent/edp/api/auth.py for the full scope/rationale).
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import HTTPException

from src.agent.edp.api.auth import ADMIN_ROLE, require_admin_role


def _fake_jwt(payload: dict) -> str:
    def _b64(obj) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(payload)}.sig"


class _FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = _FakeHeaders(headers)


def test_allows_admin_via_x_user_role_header():
    require_admin_role(_FakeRequest({"X-User-Role": ADMIN_ROLE}))  # must not raise


def test_allows_admin_via_jwt_role_claim():
    token = _fake_jwt({"role": ADMIN_ROLE, "email": "a@b.com"})
    require_admin_role(_FakeRequest({"Authorization": f"Bearer {token}"}))  # must not raise


def test_x_user_role_header_wins_over_jwt_role_claim():
    token = _fake_jwt({"role": "Viewer"})
    require_admin_role(
        _FakeRequest(
            {
                "Authorization": f"Bearer {token}",
                "X-User-Role": ADMIN_ROLE,
            }
        )
    )  # header overrides a conflicting JWT claim -- must not raise


def test_rejects_non_admin_role_with_403():
    with pytest.raises(HTTPException) as exc_info:
        require_admin_role(_FakeRequest({"X-User-Role": "Viewer"}))
    assert exc_info.value.status_code == 403
    assert "Viewer" in exc_info.value.detail


def test_rejects_missing_role_with_403():
    with pytest.raises(HTTPException) as exc_info:
        require_admin_role(_FakeRequest({}))
    assert exc_info.value.status_code == 403
    assert "unknown" in exc_info.value.detail


def test_rejects_malformed_jwt_gracefully_with_403():
    with pytest.raises(HTTPException) as exc_info:
        require_admin_role(_FakeRequest({"Authorization": "Bearer not-a-jwt"}))
    assert exc_info.value.status_code == 403
