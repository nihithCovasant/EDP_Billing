"""Repository package — re-exports all public DB operations."""

from .control import get_effective_state, record_action, get_history as get_control_history
from .workflow import (
    get_active,
    get_latest_effective,
    upload,
    get_history as get_workflow_history,
    compute_hash,
)
from .segment import (
    get_one,
    get_all_for_date,
    get_in_progress,
    get_day_summary,
    seed_from_workflow,
    acquire_lock,
    release_lock,
    recover_stale_locks,
    touch_heartbeat,
    retry_segment,
    skip_segment_manually,
)

__all__ = [
    # control
    "get_effective_state",
    "record_action",
    "get_control_history",
    # workflow
    "get_active",
    "get_latest_effective",
    "upload",
    "get_workflow_history",
    "compute_hash",
    # segment
    "get_one",
    "get_all_for_date",
    "get_in_progress",
    "get_day_summary",
    "seed_from_workflow",
    "acquire_lock",
    "release_lock",
    "recover_stale_locks",
    "touch_heartbeat",
    "retry_segment",
    "skip_segment_manually",
]
