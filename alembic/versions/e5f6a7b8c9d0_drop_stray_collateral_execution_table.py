"""drop stray collateral_execution table

collateral_execution was a standalone table (with its own collateralstatus /
collateralphase enums, one row per trade_date) that predates the decision to
model all 5 T+1 post-trade processes -- including Collateral Valuation -- as
extra rows in segment_execution (segment_code IN COLVAL/COLALLOC/MTFFT/
DMRPT/DMSTMT), sharing the same schema, enums, and pipeline as the 7 real
segments (see d4e5f6a7b8c9 and pipeline/post_trade_stages.py). It was never
referenced by any model, repository function, or migration in this codebase
and never held any data -- dropping it here so the schema matches what the
code actually uses.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLLATERAL_STATUS_VALUES = ("PENDING", "IN_PROGRESS", "COMPLETED", "SKIPPED", "FAILED")
_COLLATERAL_PHASE_VALUES = ("AWAIT_GTG", "TRIGGER_JOB", "AWAIT_CONFIRM", "DONE")


def upgrade() -> None:
    # IF EXISTS: on a fresh install this table was never part of the
    # migration chain — it only existed as a stray manual/orphan table in
    # some environments. A bare DROP TABLE aborts the whole upgrade on a
    # new database and prevents the agent from starting.
    op.execute("DROP TABLE IF EXISTS collateral_execution")
    op.execute("DROP TYPE IF EXISTS collateralphase")
    op.execute("DROP TYPE IF EXISTS collateralstatus")


def downgrade() -> None:
    # Recreates the table as it was found (empty, standalone) in case
    # anything outside this codebase still expects it to exist. Not
    # wired up to any model/repository code -- purely a schema-level
    # rollback.
    collateral_status = sa.Enum(*_COLLATERAL_STATUS_VALUES, name="collateralstatus")
    collateral_phase = sa.Enum(*_COLLATERAL_PHASE_VALUES, name="collateralphase")
    collateral_status.create(op.get_bind())
    collateral_phase.create(op.get_bind())

    op.create_table(
        "collateral_execution",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", collateral_status, nullable=False),
        sa.Column("current_phase", collateral_phase, nullable=True),
        sa.Column("lock_json", sa.JSON(), nullable=False),
        sa.Column("processes_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_category", sa.String(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="collateral_execution_pkey"),
        sa.UniqueConstraint("trade_date", name="uq_collateral_execution_trade_date"),
    )
    op.create_index(
        "ix_collateral_execution_trade_date",
        "collateral_execution",
        ["trade_date"],
    )
