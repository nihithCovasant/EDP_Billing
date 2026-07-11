"""
SQLAlchemy models for EDP Billing agent — 3 tables, all prefixed edpb_:

edpb_properties          — daily uploaded JSON config per trade_date
edpb_segment_execution   — runtime state per (trade_date, segment_code)
edpb_agent_control       — append-only START/STOP audit log

No foreign keys (soft references only). All tables are append-only.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# Wrapped with MutableDict so in-place mutation (row.x_json["k"] = v) is
# tracked correctly, not just whole-column reassignment.
_MutableJSON = MutableDict.as_mutable(JSON)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SegmentStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class SegmentState(str, enum.Enum):
    """
    There are no "phases" — only states and the actions a state's handler
    takes. Shared by two pipelines, distinguished by segment_code (see
    utils/constants.SEGMENT_ORDER vs POST_TRADE_ORDER):

    (1) Real-segment pipeline (10x: EQ/DR/CUR/SLB/NCDEX/NCDEXPHY/MCX/
        MCXPHY/NSECOM/MF) — happy flow:
      INIT -> WAITING_FOR_FILE_UPLOAD -> TRIGGERED -> WAITING_FOR_BILLPOSTING
      -> WAITING_FOR_RECON -> WAITING_FOR_CONTRACT_NOTE_GENERATION -> (SUCCEEDED)

      INIT's handler does the holiday-check operation (not a separate
      state); WAITING_FOR_FILE_UPLOAD's handler does the reserve/confirm-PID
      operation on its first entry, then polls FILEUPLOAD on later cycles
      (also not a separate state) — both are pure gate/ops-driven waits with
      no CBOS trigger involved. TRIGGERED is the one genuine crash-safety
      wait (getNewTradeProcess with the real PID). WAITING_FOR_BILLPOSTING/
      _RECON/_CONTRACT_NOTE_GENERATION are pure polls — CBOS auto-runs each
      step, the agent only observes.

    (2) Post-trade pipeline (5x: COLVAL/COLALLOC/MTFFT/DMRPT/DMSTMT) —
        happy flow:
      WAITING_FOR_GTG -> [TRIGGERED ->] WAITING_FOR_COMPLETION -> (SUCCEEDED)

      WAITING_FOR_GTG's handler polls process readiness, then (once ready)
      calls the process's "already triggered" CBOS check: if already
      triggered, it takes the direct edge straight to WAITING_FOR_COMPLETION
      (no new trigger fired); otherwise it moves to TRIGGERED, which fires
      the real trigger call next cycle.

      DMRPT and DMSTMT have no CBOS GTG/holiday-check endpoint at all —
      their "readiness" is instead purely sequential and DB-only: DMRPT
      waits for MTFFT (the previous process in POST_TRADE_ORDER) to reach
      a terminal segment_status, DMSTMT waits for DMRPT, with no CBOS call
      made for this check (see PostTradeStateMachine.DEPENDS_ON_PREVIOUS_PROCESS).
      "Terminal" includes FAILED/SKIPPED, not just COMPLETED, so a
      predecessor's failure doesn't block them forever; and since no CBOS
      call happens, neither can be individually SKIPPED for a holiday the
      way the other 3 can.

    Terminal outcomes (SUCCEEDED/FAILED/SKIPPED) are not values here — they
    live on segment_status (SegmentStatus); a row's current_state is set to
    None once segment_status reaches COMPLETED/SKIPPED, or left frozen at
    the state it was in when FAILED, for diagnostics.
    """
    # Real-segment pipeline
    INIT = "INIT"
    WAITING_FOR_FILE_UPLOAD = "WAITING_FOR_FILE_UPLOAD"
    TRIGGERED = "TRIGGERED"
    WAITING_FOR_BILLPOSTING = "WAITING_FOR_BILLPOSTING"
    WAITING_FOR_RECON = "WAITING_FOR_RECON"
    WAITING_FOR_CONTRACT_NOTE_GENERATION = "WAITING_FOR_CONTRACT_NOTE_GENERATION"

    # Post-trade pipeline (TRIGGERED shared with the real-segment pipeline —
    # same "genuine crash-safety trigger wait" concept in both).
    WAITING_FOR_GTG = "WAITING_FOR_GTG"
    WAITING_FOR_COMPLETION = "WAITING_FOR_COMPLETION"


class AgentControlAction(str, enum.Enum):
    START = "START"
    STOP = "STOP"


# ---------------------------------------------------------------------------
# Table 1: edpb_properties
# ---------------------------------------------------------------------------

class EdpProperties(Base):
    """
    Daily workflow config uploaded by MOFSL ops. One active row per
    trade_date. Every upload inserts a new row and supersedes the old one
    (is_active=False, superseded_at set) — no content-hash dedup.

    workflow_json shape (always IST, no per-config timezone field; no
    wake_interval_seconds either — that's an agent-level setting via the
    EDP_WAKE_INTERVAL_SECONDS env var, not something ops can override per upload):
    {
      "segments": [
        {
          "segment_code": "EQ",
          "login_id": "CV0001",
          "window_start": "17:00",
          "window_end": "06:00"
        },
        ...10 segments: EQ, DR, CUR, SLB, NCDEX, NCDEXPHY, MCX, MCXPHY, NSECOM, MF...
      ],
      "post_trade_processes": [...5 processes: COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT...]
    }

    Segments run same-day; window_end only rolls onto the next calendar
    day if it's chronologically at/before window_start (e.g. an overnight
    17:00->06:00 window).     Post-trade processes always gate on T+1
    regardless of the times configured — both window_start (opening gate,
    default 02:00) and window_end (closing deadline, default 06:00) are
    optional per-process overrides in "post_trade_processes", always
    resolved against trade_date+1. Both T+1 rules live in orchestrator.py,
    not fields a config uploader needs to state.

    sequence_order and segment_name are fixed code constants (see
    utils/constants.py), not stored.
    """

    __tablename__ = "edpb_properties"
    __table_args__ = (
        # DB-enforced "one active row per trade_date" — closes the
        # concurrent-upload race in repository.workflow.upload().
        Index(
            "ix_edpb_properties_one_active_per_date",
            "trade_date",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    trade_date: Mapped[date] = mapped_column(
        Date, nullable=False, index=True
    )
    workflow_json: Mapped[dict] = mapped_column(
        _MutableJSON, nullable=False,
        comment="Full segment + process config for the day"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
        comment="Only one active row per trade_date"
    )
    uploaded_by: Mapped[str] = mapped_column(
        String(256), nullable=False, default="system"
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Set when a newer config replaces this row for the same trade_date"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Table 2: edpb_segment_execution
# ---------------------------------------------------------------------------

class SegmentExecution(Base):
    """
    One row per (trade_date, segment_code) — either a real segment or a
    T+1 post-trade process; both share this table's status/lock/heartbeat
    machinery, differing only in processes_json shape and current_state.

    Every top-level processes_json key is exactly a SegmentState.value
    string — the same vocabulary current_state uses — so there is exactly
    one name for "the RECON step" (see utils/json_helpers.py's module
    docstring for the full rationale). Every state's dict (other than
    TRIGGERED) holds a "steps" sub-dict, one entry per distinct CBOS call
    that state makes, keyed by a name identifying exactly which
    endpoint/operation it recorded (e.g. "BILLPOSTING_STATUS"). A step has
    no "status" field while still pending — only last_response/
    last_checked_at accumulate; once CBOS returns TRUE/SKIP the step gets
    a completion timestamp (checked_at/ready_at/confirmed_at) and the
    *state's* own "status" becomes "COMPLETED". current_state on the row
    (not this JSON) is what actually drives control flow. No poll count is
    tracked — only the latest observed response matters.

    processes_json shape, 10 real segments (6 keys — insertion order
    matches pipeline order, since each key is only ever created when that
    state is first entered):
    {
      "INIT":                                  {"status"?: "COMPLETED", "steps": {"BeginFileUpload_STATUS": {"last_response": ..., "last_checked_at"|"checked_at": ...}}},
      "WAITING_FOR_FILE_UPLOAD":                {"status"?: "COMPLETED", "steps": {"reserve_process_id"?: {"process_id_reserved": ..., "process_id_source": "EXISTING"|"RESERVED_NEW", "reserved_at": ...}, "FILEUPLOAD_STATUS": {"last_response": ..., "last_checked_at"|"ready_at": ...}}},
      "TRIGGERED":                              {"status": ..., "at": ..., "process_id_used": ..., "process_id_source": ..., "is_runnable": bool},
      "WAITING_FOR_BILLPOSTING":                {"status"?: "COMPLETED", "steps": {"BILLPOSTING_STATUS": {"last_response": ..., "last_checked_at"|"confirmed_at": ...}}},
      "WAITING_FOR_RECON":                      {"status"?: "COMPLETED", "steps": {"RECON_STATUS": {"last_response": ..., "last_checked_at"|"confirmed_at": ...}}},
      "WAITING_FOR_CONTRACT_NOTE_GENERATION":   {"status"?: "COMPLETED", "steps": {"CONTRACTNOTEGENERATION_STATUS": {"last_response": ..., "last_checked_at"|"confirmed_at": ...}}}
    }
    "reserve_process_id" is written once, on WAITING_FOR_FILE_UPLOAD's
    first entry (before FILEUPLOAD_STATUS even exists) — nested as a step
    rather than another top-level key so processes_json's top-level keys
    stay exactly the SegmentState vocabulary; "TRIGGERED" copies
    process_id_source forward from it once TRIGGERED genuinely fires.
    TRIGGERED itself has no "steps" wrapper — it's a single atomic action
    already fully described by its own flat status/process_id/is_runnable
    fields. CBOS ProcessName -> state key: BeginFileUpload->INIT,
    FILEUPLOAD->WAITING_FOR_FILE_UPLOAD, BILLPOSTING->WAITING_FOR_BILLPOSTING,
    RECON->WAITING_FOR_RECON, CONTRACTNOTEGENERATION->WAITING_FOR_CONTRACT_NOTE_GENERATION.

    processes_json shape, 5 post-trade processes (3 keys). The step key
    embeds the resolved ProcessName (ops-configurable, e.g. "COLVAL"),
    same convention as the real-segment endpoint-named step keys. For
    DMRPT/DMSTMT specifically, WAITING_FOR_GTG's step key is instead the
    fixed "PREV_PROCESS_STATUS" — its last_response is the predecessor's
    terminal segment_status (e.g. "COMPLETED"/"FAILED"), not a CBOS reply:
    {
      "WAITING_FOR_GTG":        {"status"?: "COMPLETED", "steps": {"<ProcessName>_STATUS": {"last_response": ..., "last_checked_at"|"ready_at": ...}}},
      "TRIGGERED":               {"status": ..., "at": ..., "message": ...},
      "WAITING_FOR_COMPLETION": {"status"?: "COMPLETED", "steps": {"<ProcessName>_STATUS": {"last_response": ..., "last_checked_at"|"confirmed_at": ...}}}
    }

    current_process holds the CBOS ProcessName currently being polled
    (null while resolving the process_id, or during TRIGGERED).
    """

    __tablename__ = "edpb_segment_execution"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    segment_code: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment=(
            "Real segment (utils/constants.SEGMENT_ORDER) or post-trade "
            "process (POST_TRADE_ORDER); display name/order resolved from "
            "this code, not stored."
        )
    )

    config_id_used: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
        comment="edpb_properties.id that seeded this row — no FK constraint"
    )

    segment_status: Mapped[SegmentStatus] = mapped_column(
        Enum(SegmentStatus), nullable=False, default=SegmentStatus.PENDING
    )
    current_process: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Active CBOS ProcessName being polled"
    )
    current_state: Mapped[SegmentState | None] = mapped_column(
        Enum(SegmentState), nullable=True,
    )

    # process_id resolved once per segment-day (getdropdown or
    # getNewTradeProcess), reused for the TRIGGER call.
    process_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    process_id_reserved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    processes_json: Mapped[dict] = mapped_column(
        _MutableJSON, nullable=False, default=dict,
        comment="Per-stage state — see class docstring for shape"
    )

    # Window times are resolved live from workflow_json each cycle (see
    # orchestrator._resolve_window()), not stored, so a config re-upload
    # takes effect immediately.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Updated every cycle while IN_PROGRESS; staleness detector"
    )

    # Used for both SKIPPED and FAILED outcomes.
    skip_category: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment=(
            "SKIPPED: CBOS_SKIP | MANUAL_SKIP | "
            "FAILED: CBOS_ERROR | SYSTEM_ERROR | TIMEOUT"
        )
    )
    skip_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "trade_date", "segment_code",
            name="uq_segment_execution_per_day",
        ),
    )


# ---------------------------------------------------------------------------
# Table 3: edpb_agent_control
# ---------------------------------------------------------------------------

class AgentControl(Base):
    """
    Append-only audit log of agent START/STOP commands (market holidays,
    maintenance windows). snapshot_json captures live state at the time:
    {
      "active_segment": "EQ", "active_process": "BillPost", "active_state": "WAITING_FOR_BILLPOSTING",
      "pending_count": 5, "in_progress_count": 1, "completed_count": 1
    }
    """

    __tablename__ = "edpb_agent_control"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    action: Mapped[AgentControlAction] = mapped_column(
        Enum(AgentControlAction), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(
        String(256), nullable=False, default="system"
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_state: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="RUNNING or STOPPED"
    )
    snapshot_json: Mapped[dict | None] = mapped_column(
        _MutableJSON, nullable=True,
        comment="Runtime state snapshot at time of action"
    )
