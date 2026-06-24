# Compatibility shim — the wheel's otel_client.py imports from the monorepo path
# packages.common.src.platform_sdk.common.request_context_var
# but the installed package lives at platform_sdk.common.request_context_var.
# Re-export everything from the real module so both share the same ContextVar instances.
from platform_sdk.common.request_context_var import (
    get_request_context,
    set_request_context,
    reset_request_context,
    get_observability_client,
    set_observability_client,
    reset_observability_client,
)

__all__ = [
    "get_request_context",
    "set_request_context",
    "reset_request_context",
    "get_observability_client",
    "set_observability_client",
    "reset_observability_client",
]
