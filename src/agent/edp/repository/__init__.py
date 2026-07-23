"""Repository package — re-exports all public DB operations."""

from .audit import get_history as get_audit_history
from .audit import record_event as record_audit_event
from .control import get_effective_state, record_action
from .control import get_history as get_control_history
from .segment import (
    activate_segment_run as activate_segment_run,  # re-export: package API
)
from .segment import (
    get_all_for_date,
    get_day_summary,
    get_in_progress,
    get_one,
    get_or_create,
    has_processing_started,
    is_handled,
    is_record_exists,
    move_to_state,
    retry_segment,
    seed_from_workflow,
    seed_post_trade_processes,
    skip_segment_manually,
    touch_heartbeat,
)
from .segment import (
    get_manually_activated_rows as get_manually_activated_rows,  # re-export: package API
)
from .workflow import (
    clear_version_name,
    get_active,
    get_by_version_name,
    get_latest_effective,
    list_versions,
    move_version_name,
    upload,
)
from .workflow import (
    get_history as get_workflow_history,
)

__all__ = [
    "clear_version_name",
    # workflow
    "get_active",
    "get_all_for_date",
    "get_audit_history",
    "get_by_version_name",
    "get_control_history",
    "get_day_summary",
    # control
    "get_effective_state",
    "get_in_progress",
    "get_latest_effective",
    # segment
    "get_one",
    "get_or_create",
    "get_workflow_history",
    "has_processing_started",
    "is_handled",
    "is_record_exists",
    "list_versions",
    "move_to_state",
    "move_version_name",
    "record_action",
    # audit
    "record_audit_event",
    "retry_segment",
    "seed_from_workflow",
    "seed_post_trade_processes",
    "skip_segment_manually",
    "touch_heartbeat",
    "upload",
]
