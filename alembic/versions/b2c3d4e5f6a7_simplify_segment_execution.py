"""simplify segment_execution: drop domain/segment_name/window/runtime_health, consolidate lock into lock_json

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-02 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # 1. domain — this system is EDP-only (SETTLEMENT never shipped); drop
    #    from both tables and fold it out of the unique constraint.
    # 2. lock_json — consolidate lock columns into one JSON column.
    # 3. Drop columns superseded by lock_json / made into code constants /
    #    computed on demand.

    if is_sqlite:
        # SQLite: use batch mode for all ALTER TABLE operations.
        # For a fresh SQLite DB these lock columns will be empty, so we just
        # add lock_json with a default and drop the old columns in one batch.
        with op.batch_alter_table("segment_execution") as batch_op:
            batch_op.drop_constraint(
                "uq_segment_execution_per_day", type_="unique"
            )
            batch_op.add_column(
                sa.Column(
                    "lock_json", sa.JSON(), nullable=False,
                    server_default='{"state": "UNLOCKED", "owner": null, "acquired_at": null, "expires_at": null}',
                ),
            )
            batch_op.drop_column("domain")
            batch_op.drop_column("segment_name")
            batch_op.drop_column("lock_state")
            batch_op.drop_column("lock_owner")
            batch_op.drop_column("lock_acquired_at")
            batch_op.drop_column("lock_expires_at")
            batch_op.drop_column("runtime_health")
            batch_op.drop_column("window_start_at")
            batch_op.drop_column("window_end_at")
            batch_op.create_unique_constraint(
                "uq_segment_execution_per_day",
                ["trade_date", "segment_code"],
            )

        with op.batch_alter_table("edp_properties") as batch_op:
            batch_op.drop_column("domain")
    else:
        # PostgreSQL path (original logic)
        op.drop_constraint(
            "uq_segment_execution_per_day", "segment_execution", type_="unique"
        )
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
            "UPDATE segment_execution SET lock_json = '{\"state\": \"UNLOCKED\", "
            "\"owner\": null, \"acquired_at\": null, \"expires_at\": null}'::json "
            "WHERE lock_json IS NULL"
        )
        op.alter_column("segment_execution", "lock_json", nullable=False)

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
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if is_sqlite:
        with op.batch_alter_table("segment_execution") as batch_op:
            batch_op.drop_constraint(
                "uq_segment_execution_per_day", type_="unique"
            )
            batch_op.add_column(
                sa.Column("domain", sa.String(length=32), nullable=False, server_default="EDP"),
            )
            batch_op.add_column(
                sa.Column("segment_name", sa.String(length=64), nullable=False, server_default=""),
            )
            batch_op.add_column(
                sa.Column("lock_state", sa.String(length=16), nullable=False, server_default="UNLOCKED"),
            )
            batch_op.add_column(
                sa.Column("lock_owner", sa.String(length=256), nullable=True),
            )
            batch_op.add_column(
                sa.Column("lock_acquired_at", sa.DateTime(timezone=True), nullable=True),
            )
            batch_op.add_column(
                sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
            )
            batch_op.add_column(
                sa.Column("runtime_health", sa.String(length=16), nullable=False, server_default="ACTIVE"),
            )
            batch_op.add_column(
                sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=True),
            )
            batch_op.add_column(
                sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=True),
            )
            batch_op.drop_column("lock_json")
            batch_op.create_unique_constraint(
                "uq_segment_execution_per_day",
                ["trade_date", "domain", "segment_code"],
            )

        with op.batch_alter_table("edp_properties") as batch_op:
            batch_op.add_column(
                sa.Column("domain", sa.String(length=32), nullable=False, server_default="EDP"),
            )
    else:
        op.execute("CREATE TYPE lockstate AS ENUM ('UNLOCKED', 'LOCKED')")
        op.execute("CREATE TYPE runtimehealth AS ENUM ('ACTIVE', 'STALE', 'RECOVERED')")

        op.drop_constraint(
            "uq_segment_execution_per_day", "segment_execution", type_="unique"
        )

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
        op.add_column(
            "segment_execution", sa.Column("lock_owner", sa.String(length=256), nullable=True)
        )
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

