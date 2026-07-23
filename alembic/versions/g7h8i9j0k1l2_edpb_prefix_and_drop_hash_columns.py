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

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g7h8i9j0k1l2"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Rename tables (and their indexes, for naming consistency).
    op.rename_table("edp_properties", "edpb_properties")
    op.execute("ALTER INDEX ix_edp_properties_trade_date RENAME TO ix_edpb_properties_trade_date")
    op.execute("ALTER INDEX ix_edp_properties_one_active_per_date RENAME TO ix_edpb_properties_one_active_per_date")

    op.rename_table("segment_execution", "edpb_segment_execution")
    op.execute("ALTER INDEX ix_segment_execution_trade_date RENAME TO ix_edpb_segment_execution_trade_date")

    op.rename_table("agent_control", "edpb_agent_control")

    # 2 & 3. Drop the now-unused hash columns.
    op.drop_column("edpb_properties", "content_hash")
    op.drop_column("edpb_segment_execution", "config_hash_used")


def downgrade() -> None:
    op.add_column(
        "edpb_segment_execution",
        sa.Column("config_hash_used", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "edpb_properties",
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
    )

    op.rename_table("edpb_agent_control", "agent_control")

    op.execute("ALTER INDEX ix_edpb_segment_execution_trade_date RENAME TO ix_segment_execution_trade_date")
    op.rename_table("edpb_segment_execution", "segment_execution")

    op.execute("ALTER INDEX ix_edpb_properties_one_active_per_date RENAME TO ix_edp_properties_one_active_per_date")
    op.execute("ALTER INDEX ix_edpb_properties_trade_date RENAME TO ix_edp_properties_trade_date")
    op.rename_table("edpb_properties", "edp_properties")
