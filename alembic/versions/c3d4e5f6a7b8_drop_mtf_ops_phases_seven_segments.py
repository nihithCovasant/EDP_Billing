"""drop MTF-ops phases from segmentphase enum — MTF is now a normal 7th segment

Per the EDP Billing segment execution flow spec, the agent processes exactly
7 segments (CASH/EQ, F&O/DR, CD/CUR, SLBM/SL, MCX, NCDEX, MTF) sequentially,
each driven through the identical generic 7-step pipeline. MTF is no longer
a special virtual "MTFOPS" segment with its own 6-phase chain (Collateral
Valuation/Allocation, Fund Transfer, MTF Buy/Sell, Weekly Auto Closure) —
that chain has been removed from the code, so its now-unused SegmentPhase
enum values are dropped here too. Postgres does not support dropping enum
values directly, so this recreates the type with only the values still in
use and repoints the column at it.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PHASES = (
    "HOLIDAY_CHECK",
    "RESERVE_PID",
    "AWAIT_FILE_UPLOAD",
    "TRIGGER",
    "AWAIT_BILLPOSTING",
    "AWAIT_RECON",
    "AWAIT_CONTRACT_NOTE",
    "DONE",
)

_OLD_PHASES = _NEW_PHASES + (
    "COLLATERAL_VALUATION",
    "COLLATERAL_ALLOCATION",
    "FUND_TRANSFER",
    "MTF_BUY",
    "MTF_SELL",
    "WEEKLY_AUTO_CLOSURE",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite stores enums as plain TEXT — no type manipulation needed.
        # Just clean up any stray MTF-ops phase values (defensive).
        bind.execute(
            sa.text(
                "UPDATE segment_execution SET current_phase = 'DONE' "
                "WHERE current_phase IN ("
                "'COLLATERAL_VALUATION', 'COLLATERAL_ALLOCATION', 'FUND_TRANSFER', "
                "'MTF_BUY', 'MTF_SELL', 'WEEKLY_AUTO_CLOSURE')"
            )
        )
        return

    # PostgreSQL path
    op.execute(
        """
        UPDATE segment_execution
        SET current_phase = 'DONE'
        WHERE current_phase::text IN (
            'COLLATERAL_VALUATION', 'COLLATERAL_ALLOCATION', 'FUND_TRANSFER',
            'MTF_BUY', 'MTF_SELL', 'WEEKLY_AUTO_CLOSURE'
        )
        """
    )
    op.execute("ALTER TYPE segmentphase RENAME TO segmentphase_old")
    new_enum = sa.Enum(*_NEW_PHASES, name="segmentphase")
    new_enum.create(op.get_bind())
    op.execute(
        "ALTER TABLE segment_execution "
        "ALTER COLUMN current_phase TYPE segmentphase "
        "USING current_phase::text::segmentphase"
    )
    op.execute("DROP TYPE segmentphase_old")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return  # No enum manipulation needed on SQLite

    op.execute("ALTER TYPE segmentphase RENAME TO segmentphase_new")
    old_enum = sa.Enum(*_OLD_PHASES, name="segmentphase")
    old_enum.create(op.get_bind())
    op.execute(
        "ALTER TABLE segment_execution "
        "ALTER COLUMN current_phase TYPE segmentphase "
        "USING current_phase::text::segmentphase"
    )
    op.execute("DROP TYPE segmentphase_new")

