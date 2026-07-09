"""initial edp schema

Revision ID: 5846a2847710
Revises: 
Create Date: 2026-07-02 10:44:32.538499

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5846a2847710"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_control",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "action",
            sa.Enum("START", "STOP", name="agentcontrolaction"),
            nullable=False,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "effective_state",
            sa.String(length=32),
            nullable=False,
            comment="RUNNING or STOPPED",
        ),
        sa.Column(
            "snapshot_json",
            sa.JSON(),
            nullable=True,
            comment="Runtime state snapshot at time of action",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "segment_execution",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column(
            "domain",
            sa.String(length=32),
            nullable=False,
            comment="EDP or SETTLEMENT",
        ),
        sa.Column("segment_code", sa.String(length=32), nullable=False),
        sa.Column("segment_name", sa.String(length=64), nullable=False),
        sa.Column("sequence_order", sa.Integer(), nullable=False),
        sa.Column("config_id_used", sa.String(length=36), nullable=True),
        sa.Column("config_hash_used", sa.String(length=64), nullable=True),
        sa.Column(
            "segment_status",
            sa.Enum(
                "PENDING",
                "IN_PROGRESS",
                "COMPLETED",
                "SKIPPED",
                "FAILED",
                name="segmentstatus",
            ),
            nullable=False,
        ),
        sa.Column("current_process", sa.String(length=64), nullable=True),
        sa.Column(
            "current_phase",
            sa.Enum(
                "HOLIDAY_CHECK",
                "RESERVE_PID",
                "AWAIT_FILE_UPLOAD",
                "TRIGGER",
                "AWAIT_BILLPOSTING",
                "AWAIT_RECON",
                "AWAIT_CONTRACT_NOTE",
                "COLLATERAL_VALUATION",
                "COLLATERAL_ALLOCATION",
                "FUND_TRANSFER",
                "MTF_BUY",
                "MTF_SELL",
                "WEEKLY_AUTO_CLOSURE",
                "DONE",
                name="segmentphase",
            ),
            nullable=True,
        ),
        sa.Column("process_id", sa.String(length=64), nullable=True),
        sa.Column("process_id_reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "lock_state",
            sa.Enum("UNLOCKED", "LOCKED", name="lockstate"),
            nullable=False,
        ),
        sa.Column("lock_owner", sa.String(length=256), nullable=True),
        sa.Column("lock_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processes_json", sa.JSON(), nullable=False),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skip_category", sa.String(length=32), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("hitl_json", sa.JSON(), nullable=True),
        sa.Column(
            "runtime_health",
            sa.Enum("ACTIVE", "STALE", "RECOVERED", name="runtimehealth"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trade_date", "domain", "segment_code", name="uq_segment_execution_per_day"
        ),
    )
    op.create_index(
        op.f("ix_segment_execution_trade_date"),
        "segment_execution",
        ["trade_date"],
        unique=False,
    )
    op.create_table(
        "workflow_properties",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("workflow_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=256), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_properties_trade_date"),
        "workflow_properties",
        ["trade_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_workflow_properties_trade_date"), table_name="workflow_properties"
    )
    op.drop_table("workflow_properties")
    op.drop_index(
        op.f("ix_segment_execution_trade_date"), table_name="segment_execution"
    )
    op.drop_table("segment_execution")
    op.drop_table("agent_control")
    op.execute("DROP TYPE IF EXISTS agentcontrolaction")
    op.execute("DROP TYPE IF EXISTS segmentstatus")
    op.execute("DROP TYPE IF EXISTS segmentphase")
    op.execute("DROP TYPE IF EXISTS lockstate")
    op.execute("DROP TYPE IF EXISTS runtimehealth")
