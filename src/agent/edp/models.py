"""
SQLAlchemy models for EDP Billing agent — 4 tables, all prefixed edpb_:

edpb_properties          — daily uploaded JSON config per trade_date
edpb_segment_execution   — runtime state per (trade_date, segment_code)
edpb_agent_control       — append-only START/STOP audit log
edpb_audit_log           — append-only config-change audit log

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

    (1) Real-segment pipeline (9x): INIT -> [DOWNLOADING -> UPLOADING ->]
    WAITING_FOR_FILE_UPLOAD -> WAITING_FOR_INSTI_TRADE -> TRIGGERED ->
    WAITING_FOR_BILLPOSTING -> WAITING_FOR_RECON ->
    WAITING_FOR_CONTRACT_NOTE_GENERATION ->
    (SUCCEEDED). DOWNLOADING/UPLOADING are the engine-owned saga's left
    extension (BATCH_HANDOFF_CONTRACT.md): DOWNLOADING calls the RPA bot's
    /edpb/*/download (which finalizes a checksummed manifest), UPLOADING
    hands that manifest to the uploader's POST /batches — taken only by
    segments the bot can download (config.download_segments, MCX + EQ
    today); every other segment keeps the INIT ->
    WAITING_FOR_FILE_UPLOAD edge. WAITING_FOR_INSTI_TRADE is V6's new
    Step-10 gate (file_process_status CHECKINSTITRADE): Institutional
    Trade Transfer must confirm complete AFTER FILEUPLOAD goes TRUE and
    BEFORE the trigger — CBOS does not enforce it server-side, so this
    state is what stops a premature trigger. TRIGGERED is the one genuine
    crash-safety wait (getNewTradeProcess with the real PID); the rest
    are pure gate/poll waits — CBOS auto-runs each step, the agent only
    observes.

    (2) Post-trade pipeline (5x: COLVAL/COLALLOC/MTFFT/DMRPT/DMSTMT):
    WAITING_FOR_GTG -> [TRIGGERED ->] WAITING_FOR_COMPLETION -> (SUCCEEDED).
    WAITING_FOR_GTG polls readiness then checks "already triggered": if so,
    it takes the direct edge to WAITING_FOR_COMPLETION; otherwise TRIGGERED
    fires the real call next cycle.

    DMRPT and DMSTMT have no CBOS GTG endpoint — their readiness is
    DB-only: DMRPT waits for MTFFT to reach a terminal segment_status
    (FAILED/SKIPPED counts, not just COMPLETED), DMSTMT waits for DMRPT
    (see PostTradeStateMachine.DEPENDS_ON_PREVIOUS_PROCESS). Neither can be
    individually SKIPPED for a holiday since no CBOS call happens.

    Terminal outcomes (SUCCEEDED/FAILED/SKIPPED) live on segment_status,
    not here; current_state is set to None once COMPLETED/SKIPPED, or left
    frozen at the state it was in when FAILED, for diagnostics.
    """
    # Real-segment pipeline
    INIT = "INIT"
    DOWNLOADING = "DOWNLOADING"
    UPLOADING = "UPLOADING"
    WAITING_FOR_FILE_UPLOAD = "WAITING_FOR_FILE_UPLOAD"
    WAITING_FOR_INSTI_TRADE = "WAITING_FOR_INSTI_TRADE"
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


class AuditAction(str, enum.Enum):
    WORKFLOW_UPLOAD = "WORKFLOW_UPLOAD"
    WORKFLOW_VERSION_DELETE = "WORKFLOW_VERSION_DELETE"


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
        ...9 segments: EQ, DR, CUR, SLB, NCDEX, NCDEXPHY, MCX, MCXPHY, NSECOM...
      ],
      "post_trade_processes": [...5 processes: COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT...]
    }

    Segments run same-day; window_end only rolls onto the next calendar
    day if it's chronologically at/before window_start (e.g. an overnight
    17:00->06:00 window). Post-trade processes always gate on T+1
    regardless of the times configured — window_start/window_end are
    optional per-process overrides in "post_trade_processes", always
    resolved against trade_date+1. Both T+1 rules live in orchestrator.py.

    sequence_order and segment_name are fixed code constants (see
    utils/constants.py), not stored.

    version_name is an optional, independently-managed label: a config can
    also be "checked out" by name regardless of trade_date (e.g. re-apply
    the same holiday-season config every year). See repository.workflow's
    get_by_version_name()/move_version_name() and api/workflow.py's
    /workflow/versions/* endpoints.
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
        # DB-enforced "one owner row per version_name" — a name is a single,
        # unambiguous pointer (mirrors "only one config active at a time"),
        # not something to search history for. Case-insensitive so "Diwali"
        # and "diwali" can't both exist as distinct owners. Moving a name to
        # a different row (apply / overwrite_version=true) must clear it off
        # the old owner first — see repository.workflow.move_version_name().
        Index(
            "ix_edpb_properties_one_owner_per_version_name",
            text("lower(version_name)"),
            unique=True,
            postgresql_where=text("version_name IS NOT NULL"),
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
    version_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment=(
            "Optional human label for this config, reusable across dates. "
            "At most one row may own a given name at a time (see the "
            "case-insensitive unique index above) — applying or "
            "overwriting a name moves it here from its previous owner "
            "rather than duplicating it."
        )
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
    string (see utils/json_helpers.py for the rationale). Every state's
    dict (other than TRIGGERED) holds a "steps" sub-dict, one entry per
    distinct CBOS call, keyed by endpoint/operation name (e.g.
    "BILLPOSTING_STATUS"). A step has no "status" while pending — only
    last_response/last_checked_at accumulate; once CBOS returns TRUE/SKIP
    it gets a completion timestamp and the state's own "status" becomes
    "COMPLETED". current_state on the row (not this JSON) drives control
    flow; no poll count is tracked, only the latest response.

    processes_json shape, 9 real segments (7 keys — insertion order
    matches pipeline order, since each key is only ever created when that
    state is first entered):
    {
      "INIT":                                  {"status"?: "COMPLETED", "steps": {"BeginFileUpload_STATUS": {"last_response": ..., "last_checked_at"|"checked_at": ...}}},
      "WAITING_FOR_FILE_UPLOAD":                {"status"?: "COMPLETED", "steps": {"reserve_process_id"?: {"process_id_reserved": ..., "process_id_source": "EXISTING", "reserved_at": ...}, "FILEUPLOAD_STATUS": {"last_response": ..., "last_checked_at"|"ready_at": ...}}},
      "WAITING_FOR_INSTI_TRADE":                {"status"?: "COMPLETED", "steps": {"CHECKINSTITRADE_STATUS": {"last_response": ..., "last_checked_at"|"ready_at": ...}}},
      "TRIGGERED":                              {"status": ..., "at": ..., "process_id_used": ..., "process_id_source": ..., "is_runnable": bool},
      "WAITING_FOR_BILLPOSTING":                {"status"?: "COMPLETED", "steps": {"BILLPOSTING_STATUS": {"last_response": ..., "last_checked_at"|"confirmed_at": ...}}},
      "WAITING_FOR_RECON":                      {"status"?: "COMPLETED", "steps": {"RECON_STATUS": {"last_response": ..., "last_checked_at"|"confirmed_at": ...}}},
      "WAITING_FOR_CONTRACT_NOTE_GENERATION":   {"status"?: "COMPLETED", "steps": {"CONTRACTNOTEGENERATION_STATUS": {"last_response": ..., "last_checked_at"|"confirmed_at": ...}}}
    }
    "reserve_process_id" is written once, on WAITING_FOR_FILE_UPLOAD's
    first entry, nested as a step (keeps top-level keys exactly the
    SegmentState vocabulary); "TRIGGERED" copies process_id_source forward
    from it once it genuinely fires. TRIGGERED has no "steps" wrapper —
    it's a single atomic action described by its own flat fields. CBOS
    ProcessName -> state key: BeginFileUpload->INIT,
    FILEUPLOAD->WAITING_FOR_FILE_UPLOAD, CHECKINSTITRADE->WAITING_FOR_INSTI_TRADE,
    BILLPOSTING->WAITING_FOR_BILLPOSTING,
    RECON->WAITING_FOR_RECON, CONTRACTNOTEGENERATION->WAITING_FOR_CONTRACT_NOTE_GENERATION.

    processes_json shape, 5 post-trade processes (3 keys). The step key
    embeds the resolved ProcessName (e.g. "COLVAL"). For DMRPT/DMSTMT,
    WAITING_FOR_GTG's step key is instead the fixed "PREV_PROCESS_STATUS"
    — last_response is the predecessor's terminal segment_status, not a
    CBOS reply:
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

    # Manual (re)activation marker (wayfinder ticket 13): set by the retry /
    # POST /edp/run endpoints so the wake loop drives this row even when its
    # trade_date is NOT the active date (backfills, past-day retries). The
    # loop bypasses window gating for marked rows (logged loudly) and the
    # marker clears on any terminal transition.
    manually_activated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
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


# ---------------------------------------------------------------------------
# Table 4: edpb_audit_log
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """
    Append-only audit trail of config changes — who changed what, when.

    Scope is deliberately narrow: workflow config uploads (this includes
    the chat "quick patch" tool update_edp_segment_window, which re-uploads
    a patched config under the hood — see api/workflow.py's shared
    _upload_workflow_for_date()) and named-version deletes. Applying a
    saved version either results in a no-op (nothing changed, nothing
    logged) or funnels through that same upload path, so it needs no
    separate action of its own. This is NOT a log of every read, EDP
    segment state transition, or chat query — see edpb_segment_execution
    for runtime processing history instead.

    `actor` prefers the real caller identity from the request context
    (X-User-ID header -> OtelContextMiddleware, see
    src/middleware/claims_middleware.py) over any self-reported
    "uploaded_by" in the request body — see api/workflow.py::_resolve_actor()
    — falling back to that only when no request-scoped identity is
    available (e.g. a direct repository call in a test/script).
    """

    __tablename__ = "edpb_audit_log"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction), nullable=False)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    version_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
        comment="edpb_properties.id this event relates to — no FK constraint"
    )
    summary: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Short human-readable description, e.g. 'EQ.window_start 17:00 -> 18:00'"
    )
    changes_json: Mapped[dict] = mapped_column(
        _MutableJSON, nullable=False, default=dict,
        comment="Structured before/after diff — see api/workflow.py::diff_workflow_configs()"
    )
