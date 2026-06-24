"""
OTEL Request Context Middleware — sets up per-request tracing context.

Initialises the OTEL RequestContext (request_id, tenant, scope, app, agent_id)
from request headers so every log line and span produced during the request
carries those attributes automatically. No authentication is performed.

Context vars are reset after each request to prevent leakage across concurrent
async requests.
"""

import os
import uuid
import json
from pathlib import Path

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from cams_otel_lib import (
    Logger as logger,
    Otel_Client,
    otel_trace,
    RequestContext,
    set_request_context,
    set_observability_client,
    reset_request_context,
    reset_observability_client,
)
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
                return (
                    data.get("agent_definition", {}).get("name")
                    or data.get("name")
                    or "N/A"
                )
    except Exception:
        pass
    return "N/A"


_CONFIG_TENANT_ID = _read_config_field("tenant_id")
_CONFIG_USER_ID = _read_config_field("user_id")
_CONFIG_APP_NAME = _read_agent_name_from_config()
_AGENT_INSTANCE_ID = _read_config_field("instance_id")


class OtelContextMiddleware(BaseHTTPMiddleware):
    """
    Middleware that wires up OTEL request context for every request.

    Reads optional headers (X-Request-ID, X-Tenant-ID, X-User-ID, X-App-Name,
    X-Session-ID) and populates RequestContext so all logs and spans are
    automatically attributed.
    """

    @otel_trace
    async def dispatch(self, request: Request, call_next):
        request_context_token = None
        otel_client_token = None
        try:
            request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex.lower())
            tenant_name = request.headers.get("X-Tenant-ID") or _CONFIG_TENANT_ID or "N/A"
            scope_name = request.headers.get("X-Scope", "N/A")
            userid = request.headers.get("X-User-ID") or _CONFIG_USER_ID or "N/A"
            app_name = request.headers.get("X-App-Name") or _CONFIG_APP_NAME or "N/A"
            session_id = request.headers.get("X-Session-ID", uuid.uuid4().hex.lower())

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

        return response
