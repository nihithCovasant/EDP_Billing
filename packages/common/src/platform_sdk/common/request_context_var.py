# Compatibility shim — the wheel's otel_client.py imports from the monorepo path
# packages.common.src.platform_sdk.common.request_context_var
# but the installed package lives at platform_sdk.common.request_context_var.
# Re-export everything from the real module so both share the same ContextVar instances.
from platform_sdk.common.request_context_var import (
    get_observability_client,
    get_request_context,
    reset_observability_client,
    reset_request_context,
    set_observability_client,
    set_request_context,
)

__all__ = [
    "get_observability_client",
    "get_request_context",
    "reset_observability_client",
    "reset_request_context",
    "set_observability_client",
    "set_request_context",
]
