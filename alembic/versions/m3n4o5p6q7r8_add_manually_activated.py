"""add manually_activated marker to edpb_segment_execution

Backfill/past-day retry support (wayfinder ticket 13): the wake loop drives
only the active date's rows plus rows carrying this marker (set by the retry
and POST /edp/run endpoints, cleared on any terminal transition).

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m3n4o5p6q7r8"
down_revision: str | Sequence[str] | None = "l2m3n4o5p6q7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "edpb_segment_execution",
        sa.Column("manually_activated", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("edpb_segment_execution", "manually_activated")
