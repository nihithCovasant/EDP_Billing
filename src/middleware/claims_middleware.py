"""
OTEL Request Context Middleware — sets up per-request tracing context.

Initialises the OTEL RequestContext (request_id, tenant, scope, app, agent_id)
from request headers so every log line and span produced during the request
carries those attributes automatically. No authentication is performed here
— the CAMS gateway in front of this agent is responsible for validating the
caller's JWT before the request ever reaches us; we only decode the payload
it already vouched for (see _decode_jwt_claims()).

Context vars are reset after each request to prevent leakage across concurrent
async requests.
"""

import base64
import json
import os
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from cams_otel_lib import (
    Logger as logger,
)
from cams_otel_lib import (
    Otel_Client,
    RequestContext,
    otel_trace,
    reset_observability_client,
    reset_request_context,
    set_observability_client,
    set_request_context,
)
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.config.settings import settings


def _read_config_field(field: str, default: str = "N/A") -> str:
    """Read a field from agent_config.json, checking top-level then runtime_context."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = Path(ext) if ext else Path(__file__).parent.parent / "config" / "agent_config.json"
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = json.load(f)
                return data.get(field) or data.get("runtime_context", {}).get(field, default) or default
    except Exception:
        pass
    return default


def _read_agent_name_from_config() -> str:
    """Read agent name from agent_definition in agent_config.json."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = Path(ext) if ext else Path(__file__).parent.parent / "config" / "agent_config.json"
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = json.load(f)
                return data.get("agent_definition", {}).get("name") or data.get("name") or "N/A"
    except Exception:
        pass
    return "N/A"


_CONFIG_TENANT_ID = _read_config_field("tenant_id")
_CONFIG_USER_ID = _read_config_field("user_id")
_CONFIG_APP_NAME = _read_agent_name_from_config()
_AGENT_INSTANCE_ID = _read_config_field("instance_id")


def _decode_jwt_claims(request: Request) -> dict[str, Any]:
    """
    Decode (NOT verify) the payload of an `Authorization: Bearer <jwt>`
    header, if present. Signature verification is deliberately skipped —
    the CAMS auth gateway in front of this agent has already validated the
    token before routing the request here; we only need the claims it
    vouched for (email, sub, tenant_id, ...), same trust model as the
    original Agent-Template scaffold's ClaimsMiddleware (see
    Otel_Transformation.md). Returns {} on any missing/malformed header —
    never raises, so a bad/absent token just falls through to the existing
    X-User-ID/config fallbacks below instead of breaking the request.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return {}
    token = auth_header[7:].strip()
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


_role_context: ContextVar[str | None] = ContextVar("role_context", default=None)


def get_current_role() -> str | None:
    """
    The current request's role claim (from its Authorization JWT, or an
    X-User-Role header), if any -- set by OtelContextMiddleware.dispatch().

    Only used to let a re-entrant internal call (e.g. a chat tool in
    src/tools/edp_status.py hitting this same agent's own /edp/* API) carry
    the ORIGINAL request's role forward as an X-User-Role header, since
    that new HTTP call has no Authorization header of its own to decode.
    The actual enforcement (see src/agent/edp/api/auth.py::require_admin_role)
    reads directly from each request's own headers, not from this contextvar
    -- this getter is one of the sources it can end up reading back via that
    forwarded header, not a bypass of it.
    """
    return _role_context.get()


def _actor_from_claims(claims: dict[str, Any]) -> str | None:
    """
    Human-readable actor string for logs/audit trail: prefer email (readable),
    suffixed with the stable numeric `sub` when both are present so the
    identity stays unambiguous even if an email is later reassigned/changed.
    """
    email = claims.get("email")
    sub = claims.get("sub")
    if email and sub:
        return f"{email} (uid:{sub})"
    if email:
        return email
    if sub:
        return f"uid:{sub}"
    return None


class OtelContextMiddleware(BaseHTTPMiddleware):
    """
    Middleware that wires up OTEL request context for every request.

    Reads optional headers (X-Request-ID, X-Tenant-ID, X-User-ID, X-App-Name,
    X-Session-ID) and populates RequestContext so all logs and spans are
    automatically attributed. Falls back to decoding an Authorization: Bearer
    JWT (see _decode_jwt_claims()) when the plain X-User-ID/X-Tenant-ID
    headers aren't set — an explicit header always wins if both are present.
    """

    @otel_trace
    async def dispatch(self, request: Request, call_next):
        request_context_token = None
        otel_client_token = None
        role_context_token = None
        try:
            claims = _decode_jwt_claims(request)
            role_context_token = _role_context.set(request.headers.get("X-User-Role") or claims.get("role"))
            request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex.lower())
            tenant_name = (
                request.headers.get("X-Tenant-ID")
                or (str(claims["tenant_id"]) if claims.get("tenant_id") is not None else None)
                or _CONFIG_TENANT_ID
                or "N/A"
            )
            scope_name = request.headers.get("X-Scope") or claims.get("scope") or "N/A"
            userid = request.headers.get("X-User-ID") or _actor_from_claims(claims) or _CONFIG_USER_ID or "N/A"
            app_name = request.headers.get("X-App-Name") or _CONFIG_APP_NAME or "N/A"
            session_id = request.headers.get("X-Session-ID") or claims.get("session_id") or uuid.uuid4().hex.lower()

            request_context = RequestContext(
                request_id=request_id,
                tenant_name=tenant_name,
                scope_name=scope_name,
                userid=userid,
                app_name=app_name,
                agent_id=_AGENT_INSTANCE_ID,
                session_id=session_id,
                service_name=settings.agent_name,
            )
            request_context_token = set_request_context(request_context)

            otel_client = Otel_Client.initialize_otel_client(
                service_name=settings.agent_name,
                environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
                agent_id=_AGENT_INSTANCE_ID,
            )
            otel_client_token = set_observability_client(otel_client)
        except Exception as e:
            logger.error(f"Error setting up OTEL request context: {e}")

        response = await call_next(request)

        if request_context_token:
            reset_request_context(request_context_token)
        if otel_client_token:
            reset_observability_client(otel_client_token)
        if role_context_token:
            _role_context.reset(role_context_token)

        return response
