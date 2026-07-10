"""replace SegmentPhase (segmentphase enum, current_phase column) with SegmentState (segmentstate enum, current_state column)

Manager directive: eliminate the concept of "phases" entirely, replacing the
old 7-step/3-step SegmentPhase chains with the flat, happy-flow SegmentState
model (see models.SegmentState / state_machine/TradeSegmentTransitionFactory.py):

  Real segments:  INIT -> WAITING_FOR_FILE_UPLOAD -> TRIGGERED ->
                  WAITING_FOR_BILLPOSTING -> WAITING_FOR_RECON ->
                  WAITING_FOR_CONTRACT_NOTE_GENERATION
  Post-trade:     WAITING_FOR_GTG -> [TRIGGERED ->] WAITING_FOR_COMPLETION

HOLIDAY_CHECK and RESERVE_PID are no longer states at all — they're
operations folded into INIT and WAITING_FOR_FILE_UPLOAD's handlers. DONE is
dropped too — current_state is simply NULL once a row is COMPLETED/SKIPPED.

Dev DB, no prod data to preserve — drop and recreate rather than an
in-place value migration.

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i9j0k1l2m3n4"
down_revision: Union[str, Sequence[str], None] = "h8i9j0k1l2m3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_PHASES = (
    "HOLIDAY_CHECK", "RESERVE_PID", "AWAIT_FILE_UPLOAD", "TRIGGER",
    "AWAIT_BILLPOSTING", "AWAIT_RECON", "AWAIT_CONTRACT_NOTE",
    "AWAIT_GTG", "TRIGGER_JOB", "AWAIT_CONFIRM", "DONE",
)

_NEW_STATES = (
    "INIT", "WAITING_FOR_FILE_UPLOAD", "TRIGGERED", "WAITING_FOR_BILLPOSTING",
    "WAITING_FOR_RECON", "WAITING_FOR_CONTRACT_NOTE_GENERATION",
    "WAITING_FOR_GTG", "WAITING_FOR_COMPLETION",
)


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    with op.batch_alter_table("edpb_segment_execution") as batch_op:
        batch_op.drop_column("current_phase")

    if not is_sqlite:
        op.execute("DROP TYPE IF EXISTS segmentphase")
        segment_state = sa.Enum(*_NEW_STATES, name="segmentstate")
        segment_state.create(bind)
        col_type = segment_state
    else:
        col_type = sa.String(length=64)

    with op.batch_alter_table("edpb_segment_execution") as batch_op:
        batch_op.add_column(
            sa.Column("current_state", col_type, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    with op.batch_alter_table("edpb_segment_execution") as batch_op:
        batch_op.drop_column("current_state")

    if not is_sqlite:
        op.execute("DROP TYPE IF EXISTS segmentstate")
        segment_phase = sa.Enum(*_OLD_PHASES, name="segmentphase")
        segment_phase.create(bind)
        col_type = segment_phase
    else:
        col_type = sa.String(length=64)

    with op.batch_alter_table("edpb_segment_execution") as batch_op:
        batch_op.add_column(
            sa.Column("current_phase", col_type, nullable=True),
        )

