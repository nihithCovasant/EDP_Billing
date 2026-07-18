"""
Process-lifetime tracker of the last terminal-status alert email attempt —
feeds the "alerts" section of GET /edp/health (see __main__.py).

Deliberately in-memory only, not persisted: this agent is single-instance
(see edp_state_machine's no-pod-locking design), and a fresh process having
sent zero alerts yet is a normal state (e.g. no segment has hit a terminal
status today), not a failure — so an empty tracker is reported as
informational context, never on its own as a reason to fail the health
check.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .utils.datetime_utils import now_ist

_last_attempt_at: Optional[datetime] = None
_last_success_at: Optional[datetime] = None
_last_failure_at: Optional[datetime] = None
_last_error: Optional[str] = None


def record_alert_attempt(success: bool, error: Optional[str] = None) -> None:
    """Called by repository/segment.py::_send_terminal_alert() after every
    attempt to send a terminal-status alert email, success or failure."""
    global _last_attempt_at, _last_success_at, _last_failure_at, _last_error
    ts = now_ist()
    _last_attempt_at = ts
    if success:
        _last_success_at = ts
        _last_error = None
    else:
        _last_failure_at = ts
        _last_error = error


def get_alert_health() -> Dict[str, Any]:
    return {
        "last_attempt_at": _last_attempt_at.isoformat() if _last_attempt_at else None,
        "last_success_at": _last_success_at.isoformat() if _last_success_at else None,
        "last_failure_at": _last_failure_at.isoformat() if _last_failure_at else None,
        "last_error": _last_error,
    }
