"""
Pydantic v2 request / response schemas for the EDP Billing API.

All response schemas use model_config=ConfigDict(from_attributes=True)
so they can be built directly from SQLAlchemy ORM rows via model_validate().
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# =============================================================================
# Workflow
# =============================================================================


class WorkflowUploadRequest(BaseModel):
    # No trade_date field — server always targets "today's trading date"
    # (resolve_active_date()), or tomorrow if today's already underway.
    workflow_json: dict[str, Any]
    uploaded_by: str = "ops"
    # Required label to save this config under (e.g. "diwali_2026") — every
    # explicit upload must be named so it can be found again later via
    # GET /workflow/versions (no more nameless uploads / NULLs going
    # forward). If the name is already owned by another row, the upload is
    # rejected with 409 unless overwrite_version=True, in which case the
    # name is moved here.
    version_name: str
    overwrite_version: bool = False

    @field_validator("version_name")
    @classmethod
    def _version_name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("version_name is required and cannot be blank")
        return v


class WorkflowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    trade_date: date
    is_active: bool
    uploaded_by: str
    uploaded_at: datetime | None
    segment_count: int = 0
    # None for a legacy config with no "post_trade_processes" list at all.
    post_trade_process_count: int | None = None
    version_name: str | None = None


class WorkflowUploadResponse(WorkflowResponse):
    is_new: bool
    # True if today's trading date already had processing underway, so the
    # config was deferred to `trade_date` (+1 day) instead of applied today.
    deferred: bool = False
    # Today's trading date as resolved server-side, before any deferral.
    resolved_trade_date: date


class WorkflowDetailResponse(WorkflowResponse):
    workflow_json: dict[str, Any]
    # The date actually requested by the caller — differs from `trade_date`
    # (the row's own date) only when no config was ever uploaded for the
    # requested date and the last-uploaded-before-it config was carried
    # forward instead (see repository.get_latest_effective()).
    requested_trade_date: date | None = None
    carried_forward: bool = False


# =============================================================================
# Named workflow versions
# =============================================================================


class WorkflowVersionSummary(BaseModel):
    """One row returned by GET /workflow/versions (list) and .../{name} (get)."""

    id: str
    version_name: str
    trade_date: date
    is_active: bool
    uploaded_by: str
    uploaded_at: datetime | None
    segment_count: int = 0
    post_trade_process_count: int | None = None


class WorkflowVersionApplyRequest(BaseModel):
    uploaded_by: str = "ops"


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
    current_process: str | None = None
    current_state: str | None = None
    process_id: str | None = None
    process_id_reserved_at: str | None = None
    skip_category: str | None = None
    skip_reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    last_heartbeat_at: str | None = None
    # Computed live (not stored) — see utils/serializers._runtime_health().
    runtime_health: str = "ACTIVE"
    processes_json: dict[str, Any] = Field(default_factory=dict)


class DaySummaryResponse(BaseModel):
    trade_date: str
    total: int
    pending: int
    in_progress: int
    completed: int
    skipped: int
    failed: int
    segments: list[SegmentSummary]


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
    current_process: str | None = None
    current_state: str | None = None
    process_id: str | None = None
    process_id_reserved_at: str | None = None
    skip_category: str | None = None
    skip_reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    last_heartbeat_at: str | None = None
    # Computed live (not stored) — see utils/serializers._runtime_health().
    runtime_health: str = "ACTIVE"
    config_id_used: str | None = None
    processes_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


# =============================================================================
# Agent control
# =============================================================================


class AgentControlRequest(BaseModel):
    requested_by: str = "ops"
    reason: str | None = None


class AgentControlResponse(BaseModel):
    action: str
    effective_state: str
    requested_at: str
    requested_by: str
    reason: str | None = None


class AgentStopResponse(AgentControlResponse):
    snapshot: dict[str, Any] = Field(default_factory=dict)


class AgentStatusResponse(BaseModel):
    effective_state: str
    history: list[AgentControlResponse] = Field(default_factory=list)


# =============================================================================
# Audit log
# =============================================================================


class AuditLogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    occurred_at: datetime
    actor: str
    action: str
    trade_date: date | None = None
    version_name: str | None = None
    config_id: str | None = None
    summary: str
    changes_json: dict[str, Any] = Field(default_factory=dict)
