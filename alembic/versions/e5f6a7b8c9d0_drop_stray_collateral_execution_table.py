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
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLLATERAL_STATUS_VALUES = ("PENDING", "IN_PROGRESS", "COMPLETED", "SKIPPED", "FAILED")
_COLLATERAL_PHASE_VALUES = ("AWAIT_GTG", "TRIGGER_JOB", "AWAIT_CONFIRM", "DONE")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP TABLE IF EXISTS collateral_execution")
    if bind.dialect.name != "sqlite":
        op.execute("DROP TYPE IF EXISTS collateralphase")
        op.execute("DROP TYPE IF EXISTS collateralstatus")


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name != "sqlite":
        collateral_status = sa.Enum(*_COLLATERAL_STATUS_VALUES, name="collateralstatus")
        collateral_phase = sa.Enum(*_COLLATERAL_PHASE_VALUES, name="collateralphase")
        collateral_status.create(bind)
        collateral_phase.create(bind)
        status_col = collateral_status
        phase_col = collateral_phase
    else:
        status_col = sa.String(length=32)
        phase_col = sa.String(length=32)

    op.create_table(
        "collateral_execution",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", status_col, nullable=False),
        sa.Column("current_phase", phase_col, nullable=True),
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
        "ix_collateral_execution_trade_date", "collateral_execution", ["trade_date"],
    )

