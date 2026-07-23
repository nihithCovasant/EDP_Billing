"""add WAITING_FOR_INSTI_TRADE to the segmentstate enum

CBOS API doc V6 inserts a new Step 10 — Insti Trade Status GTG
(file_process_status ProcessName=CHECKINSTITRADE) — between the File Upload
Status check and the trade-process trigger. The doc is explicit that
triggering before Insti Trade Transfer completes "may cause pipeline step
failures", i.e. CBOS does NOT enforce the gate server-side; the caller must.
So the real-segment pipeline gains a state:

  ... -> WAITING_FOR_FILE_UPLOAD -> WAITING_FOR_INSTI_TRADE -> TRIGGERED -> ...

Rows already sitting in WAITING_FOR_FILE_UPLOAD at deploy time simply take
the new edge on their next advance; no data migration is needed.

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
"""

from typing import Sequence, Union

from alembic import op

revision: str = "n4o5p6q7r8s9"
down_revision: Union[str, Sequence[str], None] = "m3n4o5p6q7r8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL 12+ allows ALTER TYPE ... ADD VALUE inside a transaction
    # (the new value just can't be USED in the same transaction — we don't).
    op.execute("ALTER TYPE segmentstate ADD VALUE IF NOT EXISTS 'WAITING_FOR_INSTI_TRADE'")


def downgrade() -> None:
    # PostgreSQL cannot drop enum values. Rows in the new state would need
    # manual repair before any hypothetical downgrade; the value itself is
    # harmless to leave behind.
    pass
