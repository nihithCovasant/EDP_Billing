"""add version_name column + unique index to edpb_properties

Lets ops label a config with a reusable name (independent of trade_date)
and switch back to it later via GET/POST /edp/workflow/versions/* instead
of having to re-paste the full JSON. A name is a single, unambiguous
pointer -- at most one row may own a given name at a time, enforced here
with a case-insensitive partial unique index (mirrors the existing
"one active row per trade_date" index just above it in models.py).
Moving a name to a different row (apply / overwrite_version=true) clears
it off the previous owner first -- see repository.workflow.move_version_name().

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-07-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "j0k1l2m3n4o5"
down_revision: str | Sequence[str] | None = "i9j0k1l2m3n4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_edpb_properties_one_owner_per_version_name"


def upgrade() -> None:
    op.add_column(
        "edpb_properties",
        sa.Column("version_name", sa.String(length=128), nullable=True),
    )
    op.create_index(
        _INDEX_NAME,
        "edpb_properties",
        [text("lower(version_name)")],
        unique=True,
        postgresql_where=text("version_name IS NOT NULL"),
        sqlite_where=text("version_name IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="edpb_properties")
    op.drop_column("edpb_properties", "version_name")
