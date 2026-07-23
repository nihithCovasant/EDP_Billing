"""add DOWNLOADING and UPLOADING to the segmentstate enum

The engine-owned saga (BATCH_HANDOFF_CONTRACT.md) extends the real-segment
pipeline leftward: INIT -> DOWNLOADING (RPA bot full-segment download) ->
UPLOADING (hand the manifest to the uploader's POST /batches) ->
WAITING_FOR_FILE_UPLOAD -> ... Only config.download_segments (MCX + EQ
today) take the new states; everything else keeps the old edge.

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
"""

from collections.abc import Sequence

from alembic import op

revision: str = "l2m3n4o5p6q7"
down_revision: str | Sequence[str] | None = "k1l2m3n4o5p6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL 12+ allows ALTER TYPE ... ADD VALUE inside a transaction
    # (the new values just can't be USED in the same transaction — we don't).
    op.execute("ALTER TYPE segmentstate ADD VALUE IF NOT EXISTS 'DOWNLOADING'")
    op.execute("ALTER TYPE segmentstate ADD VALUE IF NOT EXISTS 'UPLOADING'")


def downgrade() -> None:
    # PostgreSQL cannot drop enum values. Rows in the new states would need
    # manual repair before any hypothetical downgrade; the values themselves
    # are harmless to leave behind.
    pass
