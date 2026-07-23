"""
Local development stub for cams-otel-lib when the private package is unavailable.
Provides no-op tracing and standard logging so the agent can run locally.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Optional, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_logger = logging.getLogger("cams_otel_lib")


class Logger:
    """Drop-in replacement using the standard library logging module."""

    @staticmethod
    def debug(msg: str, *args: Any, **kwargs: Any) -> None:
        _logger.debug(msg, *args, **kwargs)

    @staticmethod
    def info(msg: str, *args: Any, **kwargs: Any) -> None:
        _logger.info(msg, *args, **kwargs)

    @staticmethod
    def warning(msg: str, *args: Any, **kwargs: Any) -> None:
        _logger.warning(msg, *args, **kwargs)

    @staticmethod
    def error(msg: str, *args: Any, **kwargs: Any) -> None:
        _logger.error(msg, *args, **kwargs)


def otel_trace(func: F) -> F:
    """
    No-op tracing decorator for local development — returns `func`
    unchanged rather than wrapping it.

    A functools.wraps()-based wrapper looked like a safe passthrough, but
    broke FastAPI route handlers: functools.wraps() copies over
    __annotations__ (which, under `from __future__ import annotations` in
    the decorated module, are unevaluated strings like
    "WorkflowVersionApplyRequest") without also rebinding __globals__ — the
    wrapper's __globals__ stays pointed at *this* module. FastAPI resolves
    those string annotations via typing.get_type_hints(call), which looks
    names up in call.__globals__; since the real type isn't defined here,
    resolution silently fails and FastAPI falls back to treating the
    parameter as a query param instead of a request body, breaking every
    POST endpoint with a Pydantic body model (e.g. /workflow/upload,
    /workflow/versions/{name}/apply) with a 422 "Field required" error.
    Since this stub's tracing is a no-op anyway, the simplest correct fix
    is to not wrap at all.
    """
    return func


@dataclass
class RequestContext:
    request_id: str
    tenant_name: str
    scope_name: str
    userid: str
    app_name: str
    agent_id: str
    session_id: str
    service_name: str


_request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)
_observability_client: ContextVar[Any | None] = ContextVar("observability_client", default=None)


def set_request_context(ctx: RequestContext) -> Token:
    return _request_context.set(ctx)


def get_request_context() -> RequestContext | None:
    """Current request's context (set by OtelContextMiddleware), or None
    outside a request (e.g. a script/background task with no HTTP
    request). Mirrors the real cams-otel-lib's
    platform_sdk.common.request_context_var.get_request_context()."""
    return _request_context.get()


def reset_request_context(token: Token) -> None:
    _request_context.reset(token)


def set_observability_client(client: Any) -> Token:
    return _observability_client.set(client)


def reset_observability_client(token: Token) -> None:
    _observability_client.reset(token)


class Otel_Client:
    """Minimal OTEL client stub."""

    @staticmethod
    def initialize_otel_client(
        service_name: str,
        environment: str = "dev",
        agent_id: str = "N/A",
        **_: Any,
    ) -> Otel_Client:
        logging.basicConfig(level=logging.INFO)
        _logger.info(
            "OTEL stub initialized: service=%s env=%s agent_id=%s",
            service_name,
            environment,
            agent_id,
        )
        return Otel_Client()
