"""
Read/write helpers for the consolidated `lock_json` column on SegmentExecution.

lock_json shape: {"state": "LOCKED"|"UNLOCKED", "owner": str|None,
                   "acquired_at": iso str|None, "expires_at": iso str|None}

SQLAlchemy does NOT detect in-place mutations on JSON columns — always
reassign the whole dict (same rule as processes_json, see json_helpers.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..models import LockState, SegmentExecution

_EMPTY_LOCK: dict = {
    "state": LockState.UNLOCKED.value,
    "owner": None,
    "acquired_at": None,
    "expires_at": None,
}


def get_lock(row: SegmentExecution) -> dict:
    """Return a copy of the lock dict (empty/unlocked default if never set)."""
    return dict(row.lock_json) if row.lock_json else dict(_EMPTY_LOCK)


def lock_state(row: SegmentExecution) -> str:
    return get_lock(row).get("state", LockState.UNLOCKED.value)


def lock_owner(row: SegmentExecution) -> Optional[str]:
    return get_lock(row).get("owner")


def lock_expires_at(row: SegmentExecution) -> Optional[datetime]:
    raw = get_lock(row).get("expires_at")
    return datetime.fromisoformat(raw) if raw else None


def set_locked(row: SegmentExecution, owner: str, acquired_at: datetime, expires_at: datetime) -> None:
    row.lock_json = {
        "state": LockState.LOCKED.value,
        "owner": owner,
        "acquired_at": acquired_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def set_unlocked(row: SegmentExecution) -> None:
    row.lock_json = dict(_EMPTY_LOCK)
