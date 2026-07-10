"""drop lock_json column

lock_json was only ever a vestige of the old pod-to-pod locking mechanism.
That mechanism was removed when the agent moved to a single-instance
design (an IN_PROGRESS row now just resumes at its persisted
current_phase on restart), and nothing has written anything but the
default {} into this column since. It was kept around briefly for API
response backward compatibility (lock_state/lock_owner fields), but those
fields have now been dropped from the API too, so the column itself is
fully dead and safe to drop.

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-07-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h8i9j0k1l2m3"
down_revision: Union[str, Sequence[str], None] = "g7h8i9j0k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("edpb_segment_execution") as batch_op:
        batch_op.drop_column("lock_json")


def downgrade() -> None:
    with op.batch_alter_table("edpb_segment_execution") as batch_op:
        batch_op.add_column(
            sa.Column("lock_json", sa.JSON(), nullable=False, server_default="{}"),
        )

