"""replace SegmentPhase (segmentphase enum, current_phase column) with
SegmentState (segmentstate enum, current_state column)

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

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i9j0k1l2m3n4"
down_revision: str | Sequence[str] | None = "h8i9j0k1l2m3"
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
    "AWAIT_GTG",
    "TRIGGER_JOB",
    "AWAIT_CONFIRM",
    "DONE",
)

_NEW_STATES = (
    "INIT",
    "WAITING_FOR_FILE_UPLOAD",
    "TRIGGERED",
    "WAITING_FOR_BILLPOSTING",
    "WAITING_FOR_RECON",
    "WAITING_FOR_CONTRACT_NOTE_GENERATION",
    "WAITING_FOR_GTG",
    "WAITING_FOR_COMPLETION",
)


def upgrade() -> None:
    op.drop_column("edpb_segment_execution", "current_phase")
    op.execute("DROP TYPE IF EXISTS segmentphase")

    segment_state = sa.Enum(*_NEW_STATES, name="segmentstate")
    segment_state.create(op.get_bind())
    op.add_column(
        "edpb_segment_execution",
        sa.Column("current_state", segment_state, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("edpb_segment_execution", "current_state")
    op.execute("DROP TYPE IF EXISTS segmentstate")

    segment_phase = sa.Enum(*_OLD_PHASES, name="segmentphase")
    segment_phase.create(op.get_bind())
    op.add_column(
        "edpb_segment_execution",
        sa.Column("current_phase", segment_phase, nullable=True),
    )
