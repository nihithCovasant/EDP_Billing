"""add edpb_audit_log table

Append-only audit trail of config changes -- who changed what, when.
Scope: workflow config uploads (incl. the chat "quick patch" tool, which
re-uploads under the hood) and named-version deletes. See
src/agent/edp/models.py::AuditLog for the full rationale.

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-07-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k1l2m3n4o5p6"
down_revision: str | Sequence[str] | None = "j0k1l2m3n4o5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT_ACTION_ENUM = sa.Enum(
    "WORKFLOW_UPLOAD",
    "WORKFLOW_VERSION_DELETE",
    name="auditaction",
)


def upgrade() -> None:
    op.create_table(
        "edpb_audit_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("action", _AUDIT_ACTION_ENUM, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=True),
        sa.Column("version_name", sa.String(length=128), nullable=True),
        sa.Column("config_id", sa.String(length=36), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("changes_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_edpb_audit_log_occurred_at", "edpb_audit_log", ["occurred_at"])
    op.create_index("ix_edpb_audit_log_trade_date", "edpb_audit_log", ["trade_date"])


def downgrade() -> None:
    op.drop_index("ix_edpb_audit_log_trade_date", table_name="edpb_audit_log")
    op.drop_index("ix_edpb_audit_log_occurred_at", table_name="edpb_audit_log")
    op.drop_table("edpb_audit_log")
    _AUDIT_ACTION_ENUM.drop(op.get_bind(), checkfirst=True)
