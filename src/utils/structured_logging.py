"""
Structured logging utility with OpenTelemetry support.
Provides JSON-formatted logs with context propagation and request tracking.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime
from enum import StrEnum
from typing import Any

# Context variables for request tracking
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
thread_id_var: ContextVar[str | None] = ContextVar("thread_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
span_id_var: ContextVar[str | None] = ContextVar("span_id", default=None)


class LogLevel(StrEnum):
    """Log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class StructuredLogger:
    """
    Structured logger with JSON output and context propagation.

    Features:
    - JSON-formatted log output
    - OpenTelemetry compatible fields (trace_id, span_id)
    - Request context propagation (request_id, tenant_id, thread_id)
    - Consistent timestamp format (ISO 8601)
    - Severity levels matching cloud platforms

    Example:
        >>> logger = get_logger("my_module")
        >>> logger.info("User logged in", user_id="123", action="login")
        {
            "timestamp": "2026-01-23T12:59:00.123Z",
            "severity": "INFO",
            "logger": "my_module",
            "message": "User logged in",
            "user_id": "123",
            "action": "login",
            "request_id": "req-abc-123",
            "tenant_id": "acme_corp",
            "trace_id": "trace-xyz",
            "span_id": "span-456"
        }
    """

    def __init__(self, name: str, level: str = "INFO"):
        """
        Initialize structured logger.

        Args:
            name: Logger name (typically module name)
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        """
        self.name = name
        self._logger = logging.getLogger(name)
        self._logger.setLevel(getattr(logging, level.upper()))

        # Prevent duplicate handlers
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def _build_log_entry(self, severity: str, message: str, **kwargs: Any) -> dict[str, Any]:
        """
        Build structured log entry.

        Args:
            severity: Log severity level
            message: Log message
            **kwargs: Additional fields to include

        Returns:
            Dictionary with structured log data
        """
        # Base log entry with OpenTelemetry standard fields
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "severity": severity,
            "logger": self.name,
            "message": message,
        }

        # Add context from context vars
        request_id = request_id_var.get()
        if request_id:
            entry["request_id"] = request_id

        tenant_id = tenant_id_var.get()
        if tenant_id:
            entry["tenant_id"] = tenant_id

        thread_id = thread_id_var.get()
        if thread_id:
            entry["thread_id"] = thread_id

        trace_id = trace_id_var.get()
        if trace_id:
            entry["trace_id"] = trace_id

        span_id = span_id_var.get()
        if span_id:
            entry["span_id"] = span_id

        # Add custom fields
        entry.update(kwargs)

        return entry

    def _log(self, level: int, severity: str, message: str, **kwargs: Any) -> None:
        """Internal log method."""
        entry = self._build_log_entry(severity, message, **kwargs)
        self._logger.log(level, json.dumps(entry))

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log debug message."""
        self._log(logging.DEBUG, "DEBUG", message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """Log info message."""
        self._log(logging.INFO, "INFO", message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log warning message."""
        self._log(logging.WARNING, "WARNING", message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """Log error message."""
        self._log(logging.ERROR, "ERROR", message, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        """Log critical message."""
        self._log(logging.CRITICAL, "CRITICAL", message, **kwargs)

    def exception(self, message: str, exc_info: Any = True, **kwargs: Any) -> None:
        """
        Log exception with traceback.

        Args:
            message: Error message
            exc_info: Exception info (default: True captures current exception)
            **kwargs: Additional fields
        """
        import traceback

        entry = self._build_log_entry("ERROR", message, **kwargs)

        # Add exception details
        if exc_info and sys.exc_info()[0] is not None:
            exc_type, exc_value, _exc_tb = sys.exc_info()
            entry["exception"] = {
                "type": exc_type.__name__ if exc_type else None,
                "message": str(exc_value),
                "traceback": traceback.format_exc(),
            }

        self._logger.error(json.dumps(entry))


class LogContext:
    """
    Context manager for adding context to logs.

    Example:
        >>> logger = get_logger(__name__)
        >>> with LogContext(request_id="req-123", tenant_id="acme"):
        ...     logger.info("Processing request")
        # Logs will include request_id and tenant_id
    """

    def __init__(
        self,
        request_id: str | None = None,
        tenant_id: str | None = None,
        thread_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ):
        """
        Initialize log context.

        Args:
            request_id: Request identifier
            tenant_id: Tenant identifier
            thread_id: Thread/conversation identifier
            trace_id: Distributed trace identifier
            span_id: Span identifier
        """
        self.request_id = request_id
        self.tenant_id = tenant_id
        self.thread_id = thread_id
        self.trace_id = trace_id
        self.span_id = span_id

        # Store tokens for cleanup
        self._tokens = []

    def __enter__(self):
        """Enter context - set context variables."""
        if self.request_id:
            self._tokens.append(request_id_var.set(self.request_id))
        if self.tenant_id:
            self._tokens.append(tenant_id_var.set(self.tenant_id))
        if self.thread_id:
            self._tokens.append(thread_id_var.set(self.thread_id))
        if self.trace_id:
            self._tokens.append(trace_id_var.set(self.trace_id))
        if self.span_id:
            self._tokens.append(span_id_var.set(self.span_id))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context - reset context variables."""
        for token in self._tokens:
            token.var.reset(token)


# Global logger registry
_loggers: dict[str, StructuredLogger] = {}


def get_logger(name: str, level: str | None = None) -> StructuredLogger:
    """
    Get or create a structured logger.

    Args:
        name: Logger name (typically __name__)
        level: Optional log level override

    Returns:
        StructuredLogger instance
    """
    if name not in _loggers:
        from src.config.settings import settings

        log_level = level or settings.log_level
        _loggers[name] = StructuredLogger(name, log_level)
    return _loggers[name]


def configure_logging(level: str = "INFO", json_format: bool = True):
    """
    Configure global logging settings.

    Args:
        level: Default log level
        json_format: Use JSON format (True) or plain text (False)
    """
    if json_format:
        # JSON format is handled by StructuredLogger
        pass
    else:
        # Configure standard logging for non-JSON format
        logging.basicConfig(
            level=getattr(logging, level.upper()),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
