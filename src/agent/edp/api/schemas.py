"""
Pydantic v2 request / response schemas for the EDP Billing API.

All response schemas use model_config=ConfigDict(from_attributes=True)
so they can be built directly from SQLAlchemy ORM rows via model_validate().
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# =============================================================================
# Workflow
# =============================================================================

class WorkflowUploadRequest(BaseModel):
    # No trade_date field — server always targets "today's trading date"
    # (resolve_active_date()), or tomorrow if today's already underway.
    workflow_json: Dict[str, Any]
    uploaded_by: str = "ops"


class WorkflowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    trade_date: date
    is_active: bool
    uploaded_by: str
    uploaded_at: Optional[datetime]
    segment_count: int = 0
    # None for a legacy config with no "post_trade_processes" list at all.
    post_trade_process_count: Optional[int] = None


class WorkflowUploadResponse(WorkflowResponse):
    is_new: bool
    # True if today's trading date already had processing underway, so the
    # config was deferred to `trade_date` (+1 day) instead of applied today.
    deferred: bool = False
    # Today's trading date as resolved server-side, before any deferral.
    resolved_trade_date: date


class WorkflowDetailResponse(WorkflowResponse):
    workflow_json: Dict[str, Any]


# =============================================================================
# Segment summary (used inside DaySummaryResponse)
# =============================================================================

class SegmentSummary(BaseModel):
    segment_code: str
    segment_name: str
    # Computed from the fixed code constant (utils/constants.SEGMENT_ORDER),
    # not a stored/uploaded field.
    sequence_order: int
    segment_status: str
    current_process: Optional[str] = None
    current_phase: Optional[str] = None
    process_id: Optional[str] = None
    process_id_reserved_at: Optional[str] = None
    skip_category: Optional[str] = None
    skip_reason: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_heartbeat_at: Optional[str] = None
    # Computed live (not stored) — see utils/serializers._runtime_health().
    runtime_health: str = "ACTIVE"
    processes_json: Dict[str, Any] = Field(default_factory=dict)


class DaySummaryResponse(BaseModel):
    trade_date: str
    total: int
    pending: int
    in_progress: int
    completed: int
    skipped: int
    failed: int
    segments: List[SegmentSummary]


# =============================================================================
# Single segment detail
# =============================================================================

class SegmentDetailResponse(BaseModel):
    id: str
    trade_date: str
    segment_code: str
    segment_name: str
    # Computed from the fixed code constant (utils/constants.SEGMENT_ORDER),
    # not a stored/uploaded field.
    sequence_order: int
    segment_status: str
    current_process: Optional[str] = None
    current_phase: Optional[str] = None
    process_id: Optional[str] = None
    process_id_reserved_at: Optional[str] = None
    skip_category: Optional[str] = None
    skip_reason: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_heartbeat_at: Optional[str] = None
    # Computed live (not stored) — see utils/serializers._runtime_health().
    runtime_health: str = "ACTIVE"
    lock_state: str = "UNLOCKED"
    lock_owner: Optional[str] = None
    config_id_used: Optional[str] = None
    processes_json: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# =============================================================================
# Agent control
# =============================================================================

class AgentControlRequest(BaseModel):
    requested_by: str = "ops"
    reason: Optional[str] = None


class AgentControlResponse(BaseModel):
    action: str
    effective_state: str
    requested_at: str
    requested_by: str
    reason: Optional[str] = None


class AgentStopResponse(AgentControlResponse):
    snapshot: Dict[str, Any] = Field(default_factory=dict)


class AgentStatusResponse(BaseModel):
    effective_state: str
    history: List[AgentControlResponse] = Field(default_factory=list)
