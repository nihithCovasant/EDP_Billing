"""
Structured log-line builder for the EDP pipeline.

All log lines share a common shape so they are grep-friendly and easy to
parse by log aggregators (Datadog, Loki, CloudWatch, etc.).

Format convention:
  [EDP] {LEVEL_TAG} | trade_date={date} segment={code} | {message} | {extra_kv_pairs}

Usage:
  from .utils.log_fmt import edp_log, seg_log, stage_log, cbos_log

  logger.info(seg_log("EQ", "2026-07-01", "Segment started", window_start="17:00"))
  logger.info(stage_log("EQ", "HOLIDAY_CHECK", "poll", poll=3, response="FALSE"))
  logger.info(cbos_log("EQ", "BeginFileUpload", "OK", elapsed_ms=120, response="TRUE"))
"""

from __future__ import annotations

from typing import Any


def _kv(pairs: dict[str, Any]) -> str:
    """Render extra key=value pairs as a trailing string."""
    if not pairs:
        return ""
    parts = " ".join(f"{k}={v}" for k, v in pairs.items() if v is not None)
    return f" | {parts}" if parts else ""


def edp_log(message: str, **kwargs: Any) -> str:
    """Generic EDP agent-level log line."""
    return f"[EDP] {message}{_kv(kwargs)}"


def seg_log(segment: str, trade_date: Any, message: str, **kwargs: Any) -> str:
    """Segment-level log line — always carries segment + date context."""
    return (
        f"[EDP] trade_date={trade_date} segment={segment} | {message}{_kv(kwargs)}"
    )


def stage_log(
    segment: str,
    stage: str,
    message: str,
    **kwargs: Any,
) -> str:
    """Stage-level log line — carries segment + stage name."""
    return (
        f"[EDP] segment={segment} stage={stage} | {message}{_kv(kwargs)}"
    )


def cbos_log(
    segment: str,
    api: str,
    message: str,
    **kwargs: Any,
) -> str:
    """CBOS API call log line."""
    return (
        f"[CBOS] segment={segment} api={api} | {message}{_kv(kwargs)}"
    )


def elapsed(start_iso: str | None, end_iso: str | None) -> str | None:
    """
    Compute elapsed seconds between two ISO timestamps.
    Returns a formatted string like '142.3s' or None if either value is missing.
    """
    if not start_iso or not end_iso:
        return None
    try:
        from datetime import datetime
        fmt = "%Y-%m-%dT%H:%M:%S.%f%z" if "+" in end_iso or end_iso.endswith("Z") else "%Y-%m-%dT%H:%M:%S.%f"
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        secs = (e - s).total_seconds()
        return f"{secs:.1f}s"
    except Exception:
        return None
