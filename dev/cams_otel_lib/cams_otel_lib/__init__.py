"""
Local development stub for cams-otel-lib when the private package is unavailable.
Provides no-op tracing and standard logging so the agent can run locally.
"""

from __future__ import annotations

import functools
import inspect
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

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
    """No-op tracing decorator for local development."""

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return sync_wrapper  # type: ignore[return-value]


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


_request_context: ContextVar[Optional[RequestContext]] = ContextVar(
    "request_context", default=None
)
_observability_client: ContextVar[Optional[Any]] = ContextVar(
    "observability_client", default=None
)


def set_request_context(ctx: RequestContext) -> Token:
    return _request_context.set(ctx)


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
    ) -> "Otel_Client":
        logging.basicConfig(level=logging.INFO)
        _logger.info(
            "OTEL stub initialized: service=%s env=%s agent_id=%s",
            service_name,
            environment,
            agent_id,
        )
        return Otel_Client()
