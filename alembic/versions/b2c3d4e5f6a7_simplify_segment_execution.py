"""simplify segment_execution: drop domain/segment_name/window/runtime_health, consolidate lock into lock_json

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-02 16:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. domain — this system is EDP-only (SETTLEMENT never shipped); drop
    #    from both tables and fold it out of the unique constraint.
    op.drop_constraint("uq_segment_execution_per_day", "segment_execution", type_="unique")

    # 2. lock_json — consolidate lock_state/lock_owner/lock_acquired_at/
    #    lock_expires_at into one JSON column. Backfill from the existing
    #    columns before dropping them so any live locks aren't silently lost.
    op.add_column(
        "segment_execution",
        sa.Column("lock_json", sa.JSON(), nullable=True),
    )
    op.execute(
        """
        UPDATE segment_execution
        SET lock_json = json_build_object(
            'state', lock_state::text,
            'owner', lock_owner,
            'acquired_at', to_char(lock_acquired_at, 'YYYY-MM-DD"T"HH24:MI:SS.US'),
            'expires_at', to_char(lock_expires_at, 'YYYY-MM-DD"T"HH24:MI:SS.US')
        )
        """
    )
    op.execute(
        'UPDATE segment_execution SET lock_json = \'{"state": "UNLOCKED", '
        '"owner": null, "acquired_at": null, "expires_at": null}\'::json '
        "WHERE lock_json IS NULL"
    )
    op.alter_column("segment_execution", "lock_json", nullable=False)

    # 3. Drop columns superseded by lock_json / made into code constants /
    #    computed on demand.
    op.drop_column("segment_execution", "domain")
    op.drop_column("segment_execution", "segment_name")
    op.drop_column("segment_execution", "lock_state")
    op.drop_column("segment_execution", "lock_owner")
    op.drop_column("segment_execution", "lock_acquired_at")
    op.drop_column("segment_execution", "lock_expires_at")
    op.drop_column("segment_execution", "runtime_health")
    op.drop_column("segment_execution", "window_start_at")
    op.drop_column("segment_execution", "window_end_at")
    op.drop_column("edp_properties", "domain")

    op.create_unique_constraint(
        "uq_segment_execution_per_day",
        "segment_execution",
        ["trade_date", "segment_code"],
    )

    # No longer used now that lock_state/runtime_health aren't mapped Enum
    # columns.
    op.execute("DROP TYPE IF EXISTS lockstate")
    op.execute("DROP TYPE IF EXISTS runtimehealth")


def downgrade() -> None:
    op.execute("CREATE TYPE lockstate AS ENUM ('UNLOCKED', 'LOCKED')")
    op.execute("CREATE TYPE runtimehealth AS ENUM ('ACTIVE', 'STALE', 'RECOVERED')")

    op.drop_constraint("uq_segment_execution_per_day", "segment_execution", type_="unique")

    op.add_column(
        "edp_properties",
        sa.Column("domain", sa.String(length=32), nullable=False, server_default="EDP"),
    )
    op.add_column(
        "segment_execution",
        sa.Column("domain", sa.String(length=32), nullable=False, server_default="EDP"),
    )
    op.add_column(
        "segment_execution",
        sa.Column("segment_name", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "segment_execution",
        sa.Column(
            "lock_state",
            sa.Enum("UNLOCKED", "LOCKED", name="lockstate"),
            nullable=False,
            server_default="UNLOCKED",
        ),
    )
    op.add_column("segment_execution", sa.Column("lock_owner", sa.String(length=256), nullable=True))
    op.add_column(
        "segment_execution",
        sa.Column("lock_acquired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "segment_execution",
        sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "segment_execution",
        sa.Column(
            "runtime_health",
            sa.Enum("ACTIVE", "STALE", "RECOVERED", name="runtimehealth"),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.add_column(
        "segment_execution",
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "segment_execution",
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        UPDATE segment_execution
        SET lock_state = (lock_json->>'state')::lockstate,
            lock_owner = lock_json->>'owner',
            lock_acquired_at = (lock_json->>'acquired_at')::timestamptz,
            lock_expires_at = (lock_json->>'expires_at')::timestamptz
        WHERE lock_json IS NOT NULL
        """
    )

    op.drop_column("segment_execution", "lock_json")

    op.create_unique_constraint(
        "uq_segment_execution_per_day",
        "segment_execution",
        ["trade_date", "domain", "segment_code"],
    )
