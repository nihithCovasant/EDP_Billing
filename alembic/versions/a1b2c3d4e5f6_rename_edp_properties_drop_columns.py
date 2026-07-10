"""rename workflow_properties to edp_properties, drop hitl_json/sequence_order

Revision ID: a1b2c3d4e5f6
Revises: 5846a2847710
Create Date: 2026-07-02 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "5846a2847710"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename workflow_properties -> edp_properties (table + its index)
    op.rename_table("workflow_properties", "edp_properties")

    # ALTER INDEX ... RENAME is PostgreSQL-only; SQLite needs drop+create.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.drop_index("ix_workflow_properties_trade_date", table_name="edp_properties")
        op.create_index(
            "ix_edp_properties_trade_date", "edp_properties", ["trade_date"]
        )
    else:
        op.execute(
            "ALTER INDEX ix_workflow_properties_trade_date "
            "RENAME TO ix_edp_properties_trade_date"
        )

    # 2. hitl_json — no longer used; MOFSL ops handles skipped/failed segments
    #    manually, so there is no in-app alert queue to persist.
    # 3. sequence_order — processing order is now a fixed code constant
    #    (utils/constants.SEGMENT_ORDER) instead of a per-row DB value.
    #
    # SQLite requires batch mode for column drops.
    with op.batch_alter_table("segment_execution") as batch_op:
        batch_op.drop_column("hitl_json")
        batch_op.drop_column("sequence_order")


def downgrade() -> None:
    with op.batch_alter_table("segment_execution") as batch_op:
        batch_op.add_column(
            sa.Column("sequence_order", sa.Integer(), nullable=False, server_default="99"),
        )
        batch_op.add_column(
            sa.Column("hitl_json", sa.JSON(), nullable=True),
        )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.drop_index("ix_edp_properties_trade_date", table_name="edp_properties")
        op.create_index(
            "ix_workflow_properties_trade_date", "edp_properties", ["trade_date"]
        )
    else:
        op.execute(
            "ALTER INDEX ix_edp_properties_trade_date "
            "RENAME TO ix_workflow_properties_trade_date"
        )
    op.rename_table("edp_properties", "workflow_properties")

