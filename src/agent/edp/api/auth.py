"""
Role-based access control for EDP workflow endpoints.

Deliberately narrow: only the two actions that change what actually runs
today require the caller to hold the System Administrator role --
uploading a config (which sets/edits segment + post-trade window timings)
and applying a saved version (which changes which version is active).
Everything else, including deleting/un-naming a saved version (a label
change with no effect on timings or the active config -- see
models.py::AuditLog for how it's still audited), is unrestricted.

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
            detail=(f"This action requires the '{ADMIN_ROLE}' role (caller role: {role or 'unknown'})."),
        )
