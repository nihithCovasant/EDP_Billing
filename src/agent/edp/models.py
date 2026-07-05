"""
SQLAlchemy models for EDP Billing agent — final 3-table design.

All tables are prefixed with edpb_ (EDP Billing) to namespace them within
a shared database:

edpb_properties          — daily uploaded JSON config per trade_date
edpb_segment_execution   — runtime state per (trade_date, segment_code)
edpb_agent_control       — append-only START/STOP audit log

No foreign-key constraints between tables (soft references only).
All tables are append-only — no deletes.
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

# All JSON dict columns in this file are wrapped with MutableDict.as_mutable()
# as defense-in-depth: today's code is already safe because json_helpers.py /
# locking.py always reassign the whole column (row.x_json = {...}) rather than
# mutating in place, which SQLAlchemy detects without any help. But that
# safety is by convention, not enforced — a future handler doing
# row.processes_json["x"] = y directly instead would otherwise silently lose
# the write at flush time, and neither SQLite nor Postgres tests would catch
# it. MutableDict makes in-place mutation tracked correctly too, so that
# class of bug can't happen regardless of which style future code uses.
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


class SegmentPhase(str, enum.Enum):
    """
    Two pipelines share this single enum/DB column, distinguished by which
    segment_code the row belongs to (see utils/constants.SEGMENT_ORDER vs
    POST_TRADE_ORDER) — a row is only ever in phases from ONE of the two
    lists below, never a mix:

    (1) The generic 7-step pipeline, shared by all 7 real segments (CASH/EQ,
        F&O/DR, CD/CUR, SLBM/SL, MCX, NCDEX, MTF) — MTF is not special-cased,
        it runs through the exact same phases as every other segment:

      HOLIDAY_CHECK       → POST file_process_status(BeginFileUpload)           [step 1]
      RESERVE_PID         → POST getdropdown(EXISTINGPROCESSID); if not found,
                             POST getNewTradeProcess(PROCESSID="0")             [step 2]
      AWAIT_FILE_UPLOAD   → POST file_process_status(FILEUPLOAD)   — poll       [step 3]
      TRIGGER             → POST getNewTradeProcess(PROCESSID=actual)           [step 4]
      AWAIT_BILLPOSTING   → POST file_process_status(BILLPOSTING)   — poll      [step 5]
      AWAIT_RECON         → POST file_process_status(RECON)         — poll      [step 6]
      AWAIT_CONTRACT_NOTE → POST file_process_status(CONTRACTNOTEGENERATION)    [step 7]

    (2) The generic 3-step pipeline shared by all 5 T+1 post-trade processes
        (COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT) — run once per trade_date,
        sequentially, after (but independent of) the 7 real segments:

      AWAIT_GTG     → POST file_process_status(<process-specific ProcessName>) — poll
      TRIGGER_JOB   → POST <process-specific trigger endpoint>, e.g.
                       GetCollateralValuation / MTFTradeProcessFundTransfer /
                       DailyMarginReporting / DailyMarginStatements
      AWAIT_CONFIRM → POST file_process_status(<same ProcessName>) — poll again

    DONE — terminal state, shared by both pipelines.
    """
    HOLIDAY_CHECK = "HOLIDAY_CHECK"
    RESERVE_PID = "RESERVE_PID"
    AWAIT_FILE_UPLOAD = "AWAIT_FILE_UPLOAD"
    TRIGGER = "TRIGGER"
    AWAIT_BILLPOSTING = "AWAIT_BILLPOSTING"
    AWAIT_RECON = "AWAIT_RECON"
    AWAIT_CONTRACT_NOTE = "AWAIT_CONTRACT_NOTE"

    AWAIT_GTG = "AWAIT_GTG"
    TRIGGER_JOB = "TRIGGER_JOB"
    AWAIT_CONFIRM = "AWAIT_CONFIRM"

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
# Table 1: edpb_properties
# ---------------------------------------------------------------------------

class EdpProperties(Base):
    """
    Daily workflow config uploaded by MOFSL ops.
    One active row per trade_date.

    On re-upload: the old row is soft-superseded (is_active=False,
    superseded_at set) and a new row is ALWAYS inserted — every upload is
    treated as a brand-new config version, regardless of whether its
    content matches the previous one. There is no content hash / dedup
    check; ops re-uploading the same JSON by mistake just creates another
    (identical) audit row rather than being silently absorbed as a no-op.

    workflow_json shape (always IST — there is no per-config timezone
    field; see EdpBootstrapConfig.timezone for the one fixed place the
    agent's timezone is configured):
    {
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

    __tablename__ = "edpb_properties"
    __table_args__ = (
        # Enforces "at most one active row per trade_date" at the database
        # level, not just in application code. repository.workflow.upload()
        # is check-then-act (get_active() then conditionally insert) with no
        # SELECT FOR UPDATE — two concurrent uploads for the same date (a
        # manual re-upload racing an automated retry) could otherwise both
        # pass the check before either commits, leaving two is_active=True
        # rows, which then breaks get_active()'s scalar_one_or_none() with
        # MultipleResultsFound on the very next read. With this index, the
        # loser's INSERT raises IntegrityError instead — see upload()'s
        # handling of that.
        Index(
            "ix_edpb_properties_one_active_per_date",
            "trade_date",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
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
    One row per (trade_date, segment_code). Unique constraint enforced.
    segment_code is either one of the 7 real trade segments (utils/constants.
    SEGMENT_ORDER) or one of the 5 T+1 post-trade processes (utils/constants.
    POST_TRADE_ORDER) — both kinds of rows live in this same table, sharing
    all status/lock/heartbeat machinery; only the processes_json shape and
    current_phase values differ between the two.

    All runtime state for the segment lives here:
      - segment-level status, lock, timing
      - per-process state inside processes_json (shape depends on which
        pipeline the row belongs to — see below)

    processes_json shape for the 7 real segments (6 internal stages —
    identical for all 7, MTF included; there is no separate MTF-only shape):
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

    CBOS ProcessName → internal stage key mapping (7 real segments):
      BeginFileUpload        → holiday_check
      FILEUPLOAD             → file_upload_ready
      (getdropdown / getNewTradeProcess) → trigger
      BILLPOSTING            → bill_posting
      RECON                  → recon
      CONTRACTNOTEGENERATION → contract_note

    processes_json shape for the 5 post-trade processes (3 internal stages):
    {
      "gtg": {
        "status": "COMPLETED|POLLING",
        "poll_count": 2,
        "last_response": "TRUE",
        "ready_at": "2026-06-29T02:35:00Z"
      },
      "trigger": {
        "status": "TRIGGERED|FAILED",
        "at": "2026-06-29T02:35:10Z",
        "message": "Process started successfully"
      },
      "confirm": {
        "status": "CONFIRMED|POLLING",
        "poll_count": 4,
        "last_response": "TRUE",
        "confirmed_at": "2026-06-29T03:10:00Z"
      }
    }

    current_process column stores the CBOS ProcessName currently being polled
    — for the 7 real segments: "BeginFileUpload", "FILEUPLOAD", "BILLPOSTING",
    "RECON", "CONTRACTNOTEGENERATION" (null during RESERVE_PID/TRIGGER); for
    the 5 post-trade processes: the process-specific GTG ProcessName (e.g.
    "CollateralValuation") for both AWAIT_GTG and AWAIT_CONFIRM (null during
    TRIGGER_JOB).
    """

    __tablename__ = "edpb_segment_execution"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # --- Identity ---
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    segment_code: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment=(
            "Exact CBOS API param: either a real segment — EQ, DR, CUR, SL, "
            "MCX, NCDEX, MTF (see utils/constants.SEGMENT_ORDER) — or a T+1 "
            "post-trade process — COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT "
            "(see utils/constants.POST_TRADE_ORDER). Human display name and "
            "processing order are both resolved from this code via "
            "utils/constants.get_segment_name()/get_sequence_order() — not stored."
        )
    )

    # --- Soft reference to edpb_properties (no FK) ---
    config_id_used: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
        comment="edpb_properties.id that seeded this row — no FK constraint"
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
        _MutableJSON, nullable=False, default=dict,
        comment="Lock state — see utils/locking.py for read/write helpers"
    )

    # --- Per-process state (all 4 processes in one JSON column) ---
    processes_json: Mapped[dict] = mapped_column(
        _MutableJSON, nullable=False, default=dict,
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
    # Despite the column name, this is used for BOTH SKIPPED and FAILED
    # outcomes (see pipeline.stages._skip / _fail) — an unconstrained free
    # string, not a DB enum, deliberately: MOFSL ops reads it via the status
    # API, it isn't machine-branched on anywhere in this codebase.
    skip_category: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment=(
            "SKIPPED: CBOS_SKIP | TIMEOUT | MANUAL_SKIP | AGENT_RESTART | "
            "FAILED: CBOS_ERROR | SYSTEM_ERROR"
        )
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
# Table 3: edpb_agent_control
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
