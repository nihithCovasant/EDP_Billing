"""add AWAIT_GTG/TRIGGER_JOB/AWAIT_CONFIRM phases for the 5 T+1 post-trade processes

Per the EDP Billing post-trade execution flow spec, 5 processes (Collateral
Valuation, Collateral Allocation, MTF Fund Transfer, Daily Margin Reporting,
Daily Margin Statements) run once per trade_date, after the 7 real segments,
through a generic 3-step pipeline (GTG poll -> trigger -> confirm poll).
They are stored as extra segment_execution rows (segment_code IN
COLVAL/COLALLOC/MTFFT/DMRPT/DMSTMT) sharing the same segmentphase enum/column
as the 7 real segments, so 3 new phase values are added here.

Postgres supports adding enum values directly (no need to recreate the type,
unlike removing values — see c3d4e5f6a7b8 for that pattern).

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PHASES = (
    "HOLIDAY_CHECK",
    "RESERVE_PID",
    "AWAIT_FILE_UPLOAD",
    "TRIGGER",
    "AWAIT_BILLPOSTING",
    "AWAIT_RECON",
    "AWAIT_CONTRACT_NOTE",
    "DONE",
)

_NEW_PHASES = (
    "HOLIDAY_CHECK",
    "RESERVE_PID",
    "AWAIT_FILE_UPLOAD",
    "TRIGGER",
    "AWAIT_BILLPOSTING",
    "AWAIT_RECON",
    "AWAIT_CONTRACT_NOTE",
    "AWAIT_GTG",
    "TRIGGER_JOB",
    "AWAIT_CONFIRM",
    "DONE",
)


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside the same transaction as a
    # statement that uses the new value, but adding it on its own is fine
    # even inside alembic's per-migration transaction on PG12+.
    op.execute("ALTER TYPE segmentphase ADD VALUE IF NOT EXISTS 'AWAIT_GTG'")
    op.execute("ALTER TYPE segmentphase ADD VALUE IF NOT EXISTS 'TRIGGER_JOB'")
    op.execute("ALTER TYPE segmentphase ADD VALUE IF NOT EXISTS 'AWAIT_CONFIRM'")


def downgrade() -> None:
    # Postgres can't drop enum values directly — recreate the type without
    # them (same pattern as c3d4e5f6a7b8), first clearing any row that
    # landed on one of the 3 post-trade-only phases (there won't be any if
    # the post-trade feature is being fully rolled back, but guard anyway).
    op.execute(
        """
        UPDATE segment_execution
        SET current_phase = 'DONE'
        WHERE current_phase::text IN ('AWAIT_GTG', 'TRIGGER_JOB', 'AWAIT_CONFIRM')
        """
    )

    op.execute("ALTER TYPE segmentphase RENAME TO segmentphase_new")
    old_enum = sa.Enum(*_OLD_PHASES, name="segmentphase")
    old_enum.create(op.get_bind())

    op.execute(
        "ALTER TABLE segment_execution "
        "ALTER COLUMN current_phase TYPE segmentphase "
        "USING current_phase::text::segmentphase"
    )
    op.execute("DROP TYPE segmentphase_new")
