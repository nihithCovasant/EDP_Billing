"""unique partial index: one active edp_properties row per trade_date

repository.workflow.upload() is check-then-act (get_active() then
conditionally insert) with no SELECT FOR UPDATE and no DB-level constraint
enforcing "at most one active row per trade_date". Two concurrent uploads
for the same date (a manual re-upload racing an automated retry) could
both pass the check before either commits, leaving two is_active=True rows
-- which then breaks get_active()'s scalar_one_or_none() on every future
read with MultipleResultsFound.

This adds a unique index on trade_date, filtered to is_active=true, so the
database itself rejects a second concurrent active row for the same date
(the losing INSERT raises IntegrityError, handled gracefully in
upload() by returning whichever row actually won instead of crashing).

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_edp_properties_one_active_per_date"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "edp_properties",
        ["trade_date"],
        unique=True,
        postgresql_where=text("is_active"),
        sqlite_where=text("is_active"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="edp_properties")
