"""prefix all tables with edpb_, drop content_hash / config_hash_used

Three related cleanups, all in one migration since they land in the same
deploy:

1. Every table gets an `edpb_` (EDP Billing) prefix, to namespace this
   agent's tables within a shared database:
     edp_properties     -> edpb_properties
     segment_execution  -> edpb_segment_execution
     agent_control      -> edpb_agent_control

2. edpb_properties.content_hash is dropped. repository.workflow.upload()
   no longer hashes workflow_json to dedup identical re-uploads — every
   upload now unconditionally creates a new row (see repository/workflow.py
   upload()), so the hash column serves no purpose any more.

3. edpb_segment_execution.config_hash_used is dropped for the same reason
   — it was only ever compared against content_hash for audit/reconciliation
   purposes; config_id_used (kept) is sufficient to trace which config
   version seeded a row.

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g7h8i9j0k1l2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # 1. Rename tables
    op.rename_table("edp_properties", "edpb_properties")
    op.rename_table("segment_execution", "edpb_segment_execution")
    op.rename_table("agent_control", "edpb_agent_control")

    # Rename indexes
    if is_sqlite:
        op.drop_index("ix_edp_properties_trade_date", table_name="edpb_properties")
        op.create_index("ix_edpb_properties_trade_date", "edpb_properties", ["trade_date"])

        op.drop_index("ix_edp_properties_one_active_per_date", table_name="edpb_properties")
        op.create_index(
            "ix_edpb_properties_one_active_per_date", "edpb_properties",
            ["trade_date"], unique=True,
            sqlite_where=sa.text("is_active"),
        )

        op.drop_index("ix_segment_execution_trade_date", table_name="edpb_segment_execution")
        op.create_index("ix_edpb_segment_execution_trade_date", "edpb_segment_execution", ["trade_date"])
    else:
        op.execute(
            "ALTER INDEX ix_edp_properties_trade_date "
            "RENAME TO ix_edpb_properties_trade_date"
        )
        op.execute(
            "ALTER INDEX ix_edp_properties_one_active_per_date "
            "RENAME TO ix_edpb_properties_one_active_per_date"
        )
        op.execute(
            "ALTER INDEX ix_segment_execution_trade_date "
            "RENAME TO ix_edpb_segment_execution_trade_date"
        )

    # 2 & 3. Drop the now-unused hash columns.
    if is_sqlite:
        with op.batch_alter_table("edpb_properties") as batch_op:
            batch_op.drop_column("content_hash")
        with op.batch_alter_table("edpb_segment_execution") as batch_op:
            batch_op.drop_column("config_hash_used")
    else:
        op.drop_column("edpb_properties", "content_hash")
        op.drop_column("edpb_segment_execution", "config_hash_used")


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if is_sqlite:
        with op.batch_alter_table("edpb_segment_execution") as batch_op:
            batch_op.add_column(
                sa.Column("config_hash_used", sa.String(length=64), nullable=True),
            )
        with op.batch_alter_table("edpb_properties") as batch_op:
            batch_op.add_column(
                sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
            )
    else:
        op.add_column(
            "edpb_segment_execution",
            sa.Column("config_hash_used", sa.String(length=64), nullable=True),
        )
        op.add_column(
            "edpb_properties",
            sa.Column(
                "content_hash", sa.String(length=64), nullable=False, server_default=""
            ),
        )

    op.rename_table("edpb_agent_control", "agent_control")
    op.rename_table("edpb_segment_execution", "segment_execution")
    op.rename_table("edpb_properties", "edp_properties")

    if is_sqlite:
        op.drop_index("ix_edpb_segment_execution_trade_date", table_name="segment_execution")
        op.create_index("ix_segment_execution_trade_date", "segment_execution", ["trade_date"])

        op.drop_index("ix_edpb_properties_one_active_per_date", table_name="edp_properties")
        op.create_index(
            "ix_edp_properties_one_active_per_date", "edp_properties",
            ["trade_date"], unique=True,
            sqlite_where=sa.text("is_active"),
        )

        op.drop_index("ix_edpb_properties_trade_date", table_name="edp_properties")
        op.create_index("ix_edp_properties_trade_date", "edp_properties", ["trade_date"])
    else:
        op.execute(
            "ALTER INDEX ix_edpb_segment_execution_trade_date "
            "RENAME TO ix_segment_execution_trade_date"
        )
        op.execute(
            "ALTER INDEX ix_edpb_properties_one_active_per_date "
            "RENAME TO ix_edp_properties_one_active_per_date"
        )
        op.execute(
            "ALTER INDEX ix_edpb_properties_trade_date "
            "RENAME TO ix_edp_properties_trade_date"
        )

