"""
SQLAlchemy models for EDP Billing agent — final 3-table design.

edp_properties     — daily uploaded JSON config per trade_date
segment_execution  — runtime state per (trade_date, segment_code)
agent_control      — append-only START/STOP audit log

No foreign-key constraints between tables (soft references only).
All tables are append-only — no deletes.

NOTE: this system is EDP-only (no multi-domain/SETTLEMENT support) — the
domain column that used to exist on both tables was dropped since it was
never anything but "EDP" in practice.
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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


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


class SegmentPhase(str, enum.Enum):
    """
    One generic 7-step pipeline, shared by all 7 segments (CASH/EQ, F&O/DR,
    CD/CUR, SLBM/SL, MCX, NCDEX, MTF) — MTF is not special-cased, it runs
    through the exact same phases as every other segment:

      HOLIDAY_CHECK       → POST file_process_status(BeginFileUpload)           [step 1]
      RESERVE_PID         → POST getdropdown(EXISTINGPROCESSID); if not found,
                             POST getNewTradeProcess(PROCESSID="0")             [step 2]
      AWAIT_FILE_UPLOAD   → POST file_process_status(FILEUPLOAD)   — poll       [step 3]
      TRIGGER             → POST getNewTradeProcess(PROCESSID=actual)           [step 4]
      AWAIT_BILLPOSTING   → POST file_process_status(BILLPOSTING)   — poll      [step 5]
      AWAIT_RECON         → POST file_process_status(RECON)         — poll      [step 6]
      AWAIT_CONTRACT_NOTE → POST file_process_status(CONTRACTNOTEGENERATION)    [step 7]

    DONE — terminal state.
    """
    HOLIDAY_CHECK = "HOLIDAY_CHECK"
    RESERVE_PID = "RESERVE_PID"
    AWAIT_FILE_UPLOAD = "AWAIT_FILE_UPLOAD"
    TRIGGER = "TRIGGER"
    AWAIT_BILLPOSTING = "AWAIT_BILLPOSTING"
    AWAIT_RECON = "AWAIT_RECON"
    AWAIT_CONTRACT_NOTE = "AWAIT_CONTRACT_NOTE"

    DONE = "DONE"


class LockState(str, enum.Enum):
    """
    Values used inside the lock_json column (not a mapped Enum column —
    lock_state/lock_owner/lock_acquired_at/lock_expires_at were consolidated
    into one JSON field; this enum just gives the two valid "state" values
    a name in Python code).
    """
    UNLOCKED = "UNLOCKED"
    LOCKED = "LOCKED"


class AgentControlAction(str, enum.Enum):
    START = "START"
    STOP = "STOP"


# ---------------------------------------------------------------------------
# Table 1: edp_properties
# ---------------------------------------------------------------------------

class EdpProperties(Base):
    """
    Daily workflow config uploaded by MOFSL ops.
    One active row per trade_date.

    On re-upload: old row is soft-superseded (is_active=False, superseded_at set)
    and a new row is inserted. If content_hash is identical, upload is a no-op.

    workflow_json shape:
    {
      "timezone": "Asia/Kolkata",
      "wake_interval_seconds": 60,
      "segments": [
        {
          "segment_code": "EQ",
          "login_id": "CV0001",
          "window_start": "17:00",
          "window_end": "18:00",
          "window_end_next_day": false,
          "processes": [
            {"name": "fileupload",   "order": 1, "requires_trigger": false,
             "poll_deadline": "18:00", "poll_deadline_next_day": false},
            {"name": "BillPost",     "order": 2, "requires_trigger": true,
             "poll_deadline": "18:30", "poll_deadline_next_day": false},
            {"name": "Reconn",       "order": 3, "requires_trigger": true,
             "poll_deadline": "19:00", "poll_deadline_next_day": false},
            {"name": "ContractNote", "order": 4, "requires_trigger": true,
             "poll_deadline": "19:30", "poll_deadline_next_day": false}
          ]
        },
        ...7 segments: EQ, DR, CUR, SL, MCX, NCDEX, MTF...
      ]
    }

    NOTE: segments no longer carry sequence_order or segment_name — both are
    fixed code constants resolved from segment_code (see utils/constants.py).
    """

    __tablename__ = "edp_properties"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    trade_date: Mapped[date] = mapped_column(
        Date, nullable=False, index=True
    )
    workflow_json: Mapped[dict] = mapped_column(
        JSON, nullable=False,
        comment="Full segment + process config for the day"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="SHA-256 of workflow_json; identical re-upload is a no-op"
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
# Table 2: segment_execution
# ---------------------------------------------------------------------------

class SegmentExecution(Base):
    """
    One row per (trade_date, segment_code). Unique constraint enforced.

    All runtime state for the segment lives here:
      - segment-level status, lock, timing
      - per-process state inside processes_json (holiday_check/file_upload_ready/trigger/bill_posting/recon/contract_note)

    processes_json shape (6 internal stages per segment — identical for all
    7 segments, MTF included; there is no separate MTF-only shape anymore):
    {
      "holiday_check": {
        "status": "COMPLETED|SKIPPED",
        "poll_count": 1,
        "last_response": "FALSE",      <- BeginFileUpload returned FALSE (not holiday)
        "checked_at": "2026-06-28T17:00:00Z"
      },
      "file_upload_ready": {
        "status": "COMPLETED|POLLING|TIMED_OUT",
        "poll_count": 15,
        "last_response": "TRUE",
        "ready_at": "2026-06-28T17:22:00Z"
      },
      "trigger": {
        "status": "TRIGGERED|FAILED",
        "at": "2026-06-28T17:22:30Z",
        "process_id_used": "17658",
        "process_id_source": "EXISTING|RESERVED_NEW",
        "is_runnable": true
      },
      "bill_posting": {
        "status": "CONFIRMED|POLLING|TIMED_OUT",
        "poll_count": 8,
        "last_response": "TRUE",
        "confirmed_at": "2026-06-28T18:30:00Z"
      },
      "recon": {
        "status": "CONFIRMED|POLLING|TIMED_OUT",
        "poll_count": 3,
        "last_response": "TRUE",
        "confirmed_at": "2026-06-28T19:10:00Z"
      },
      "contract_note": {
        "status": "CONFIRMED|POLLING|TIMED_OUT",
        "poll_count": 5,
        "last_response": "TRUE",
        "confirmed_at": "2026-06-28T19:45:00Z"
      }
    }

    CBOS ProcessName → internal stage key mapping:
      BeginFileUpload        → holiday_check
      FILEUPLOAD             → file_upload_ready
      (getdropdown / getNewTradeProcess) → trigger
      BILLPOSTING            → bill_posting
      RECON                  → recon
      CONTRACTNOTEGENERATION → contract_note

    current_process column stores the CBOS ProcessName currently being polled
    (e.g. "BeginFileUpload", "FILEUPLOAD", "BILLPOSTING", "RECON", "CONTRACTNOTEGENERATION")
    or null during RESERVE_PID and TRIGGER phases.
    """

    __tablename__ = "segment_execution"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # --- Identity ---
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    segment_code: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment=(
            "Exact CBOS API param: EQ, DR, CUR, SL, MCX, NCDEX, or MTF "
            "(see utils/constants.SEGMENT_ORDER). Human display name and "
            "processing order are both resolved from this code via "
            "utils/constants.get_segment_name()/get_sequence_order() — not stored."
        )
    )

    # --- Soft reference to edp_properties (no FK) ---
    config_id_used: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
        comment="edp_properties.id that seeded this row — no FK constraint"
    )
    config_hash_used: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="edp_properties.content_hash at seed time — for audit"
    )

    # --- Overall segment status ---
    segment_status: Mapped[SegmentStatus] = mapped_column(
        Enum(SegmentStatus), nullable=False, default=SegmentStatus.PENDING
    )
    current_process: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Active CBOS ProcessName being polled: BeginFileUpload | FILEUPLOAD | BILLPOSTING | RECON | CONTRACTNOTEGENERATION"
    )
    current_phase: Mapped[SegmentPhase | None] = mapped_column(
        Enum(SegmentPhase), nullable=True,
        comment="Active phase: READINESS | RESERVE_PID | TRIGGER | CONFIRM | DONE"
    )

    # --- CBOS process_id (resolved once per segment-day, reused for trigger) ---
    process_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment=(
            "Resolved once per segment-day — either found via getdropdown"
            "(EXISTINGPROCESSID) or reserved via getNewTradeProcess(PROCESSID='0')"
            " if none existed yet; reused for the TRIGGER call"
        )
    )
    process_id_reserved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When the process_id was resolved (found existing or newly reserved)"
    )

    # --- Lock (prevents double-trigger across restarts/pods) ---
    # Consolidated from 4 columns (lock_state/lock_owner/lock_acquired_at/
    # lock_expires_at) into one JSON blob — always read/written as a unit,
    # never queried by more than one field, and the one place that used to
    # filter on lock_state+lock_expires_at (recover_stale_locks) scans the
    # whole table anyway since it's small (<=9 rows/day).
    # Shape: {"state": "LOCKED"|"UNLOCKED", "owner": str|None,
    #         "acquired_at": iso str|None, "expires_at": iso str|None}
    lock_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="Lock state — see utils/locking.py for read/write helpers"
    )

    # --- Per-process state (all 4 processes in one JSON column) ---
    processes_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="Keys: holiday_check, file_upload_ready, trigger, bill_posting, recon, contract_note — see docstring for shape"
    )

    # --- Timing ---
    # window_start_at / window_end_at used to be columns computed once at
    # seed time from workflow_json. They're pure functions of
    # (workflow_json, trade_date, timezone) so they're now resolved on
    # demand via orchestrator._resolve_window() instead of stored — this
    # also means a config re-upload takes effect immediately instead of
    # only for not-yet-started (PENDING) segments.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When segment moved from PENDING to IN_PROGRESS"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Set on any terminal state: COMPLETED, FAILED, or SKIPPED"
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Updated every orchestrator cycle while IN_PROGRESS; staleness detector"
    )

    # --- Skip / Failure reason ---
    skip_category: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="CBOS_SKIP | TIMEOUT | MANUAL_SKIP | HOLIDAY | DEPENDENCY_FAILED"
    )
    skip_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    # NOTE: "AGENT_RESTART" is a skip_category value — if the agent process
    # crashes/restarts while a segment is IN_PROGRESS, recover_stale_locks()
    # marks it SKIPPED (not resumed) with this category. There is no
    # "runtime_health"/RECOVERED column anymore; a STALE heartbeat is
    # computed live at read time (see utils/serializers.py), not persisted.

    # --- Audit ---
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
# Table 3: agent_control
# ---------------------------------------------------------------------------

class AgentControl(Base):
    """
    Append-only audit log of agent START / STOP commands.

    Never updated — only new rows are inserted.
    Used for market holidays and maintenance windows.

    snapshot_json captures the live state at the moment of STOP/START:
    {
      "active_segment": "EQ",
      "active_process": "BillPost",
      "active_phase": "CONFIRM",
      "pending_count": 5,
      "in_progress_count": 1,
      "completed_count": 1
    }
    """

    __tablename__ = "agent_control"

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
        JSON, nullable=True,
        comment="Runtime state snapshot at time of action"
    )
