"""
Role-based access control for mutating EDP workflow endpoints.

Config changes (upload, apply a saved version, delete a saved version --
the same "config changes" scope as the edpb_audit_log trail, see
models.py::AuditLog) require the caller to hold the System Administrator
role. Read-only endpoints (status, list/get versions, audit log) are
unrestricted.

Reads directly from the request's own headers on every call -- an
Authorization: Bearer JWT's `role` claim (decoded, not verified; see
src/middleware/claims_middleware.py::_decode_jwt_claims for the trust
model), or an explicit X-User-Role header. This does NOT depend on
OtelContextMiddleware having run first, so it works correctly for both:
- an external caller hitting /edp/workflow/* directly with their own JWT.
- a chat tool's internal re-entrant call to this same agent's own API
  (see src/tools/edp_status.py), which has no Authorization header of its
  own but forwards X-User-Role from the ORIGINAL request's role via
  claims_middleware.get_current_role().
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from src.middleware.claims_middleware import _decode_jwt_claims

ADMIN_ROLE = "System Administrator"


def require_admin_role(request: Request) -> None:
    role = request.headers.get("X-User-Role") or _decode_jwt_claims(request).get("role")
    if role != ADMIN_ROLE:
        raise HTTPException(
            status_code=403,
            detail=(
                f"This action requires the '{ADMIN_ROLE}' role "
                f"(caller role: {role or 'unknown'})."
            ),
        )
