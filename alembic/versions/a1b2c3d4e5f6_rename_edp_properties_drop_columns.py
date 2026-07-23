"""rename workflow_properties to edp_properties, drop hitl_json/sequence_order

Revision ID: a1b2c3d4e5f6
Revises: 5846a2847710
Create Date: 2026-07-02 15:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "5846a2847710"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Rename workflow_properties -> edp_properties (table + its index)
    op.rename_table("workflow_properties", "edp_properties")
    op.execute("ALTER INDEX ix_workflow_properties_trade_date RENAME TO ix_edp_properties_trade_date")

    # 2. hitl_json — no longer used; MOFSL ops handles skipped/failed segments
    #    manually, so there is no in-app alert queue to persist.
    op.drop_column("segment_execution", "hitl_json")

    # 3. sequence_order — processing order is now a fixed code constant
    #    (utils/constants.SEGMENT_ORDER) instead of a per-row DB value.
    op.drop_column("segment_execution", "sequence_order")


def downgrade() -> None:
    op.add_column(
        "segment_execution",
        sa.Column("sequence_order", sa.Integer(), nullable=False, server_default="99"),
    )
    op.add_column(
        "segment_execution",
        sa.Column("hitl_json", sa.JSON(), nullable=True),
    )

    op.execute("ALTER INDEX ix_edp_properties_trade_date RENAME TO ix_workflow_properties_trade_date")
    op.rename_table("edp_properties", "workflow_properties")
