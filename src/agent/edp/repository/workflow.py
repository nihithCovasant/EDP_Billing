"""
edp_properties table — daily config upload and retrieval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EdpProperties
from ..utils.datetime_utils import now_ist
from cams_otel_lib import Logger as logger, otel_trace


def compute_hash(workflow_json: dict) -> str:
    """SHA-256 of the canonically serialized workflow JSON."""
    serialized = json.dumps(workflow_json, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


@otel_trace
async def get_active(
    session: AsyncSession,
    trade_date: date,
) -> Optional[EdpProperties]:
    """Return the currently active workflow config for a given date."""
    stmt = select(EdpProperties).where(
        EdpProperties.trade_date == trade_date,
        EdpProperties.is_active.is_(True),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@otel_trace
async def get_latest_effective(
    session: AsyncSession,
    as_of_date: date,
) -> Optional[EdpProperties]:
    """
    Return the most recently uploaded active config on or before `as_of_date`.

    Ops does NOT need to upload a config every day — only when something
    changes. This carries the last uploaded config forward indefinitely
    until a newer one is uploaded (for any date, not just `as_of_date`).

    Used as a fallback by the orchestrator when get_active(as_of_date) finds
    no row uploaded specifically for today.
    """
    stmt = (
        select(EdpProperties)
        .where(
            EdpProperties.is_active.is_(True),
            EdpProperties.trade_date <= as_of_date,
        )
        .order_by(
            EdpProperties.trade_date.desc(),
            EdpProperties.uploaded_at.desc(),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@otel_trace
async def upload(
    session: AsyncSession,
    trade_date: date,
    workflow_json: dict,
    uploaded_by: str = "system",
) -> tuple[EdpProperties, bool]:
    """
    Insert or replace the workflow config for the day.

    Returns (row, is_new):
      is_new = False → identical config already active, no change made.
      is_new = True  → new config row created (old row superseded if present).

    Concurrency: this is check-then-act (get_active() then conditionally
    insert), but a unique partial index — one active row per trade_date,
    see models.EdpProperties.__table_args__ — makes the actual write
    atomic at the database level. Two concurrent uploads for the same date
    (a manual re-upload racing an automated retry) can both pass the
    get_active() check before either commits, but only one INSERT can ever
    land; the other raises IntegrityError, caught below and resolved by
    returning whichever row actually won instead of crashing the request or
    leaving two is_active=True rows (which would break every future
    get_active() call with MultipleResultsFound).
    """
    new_hash = compute_hash(workflow_json)
    existing = await get_active(session, trade_date)

    if existing and existing.content_hash == new_hash:
        logger.info(f"Workflow upload: identical hash — no change for {trade_date}")
        return existing, False

    ts = now_ist()
    if existing:
        existing.is_active = False
        existing.superseded_at = ts
        logger.info(f"Workflow superseded: id={existing.id} for {trade_date}")

    new_row = EdpProperties(
        trade_date=trade_date,
        workflow_json=workflow_json,
        content_hash=new_hash,
        is_active=True,
        uploaded_by=uploaded_by,
        uploaded_at=ts,
    )
    session.add(new_row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        logger.warning(
            f"Workflow upload: lost a concurrent-upload race for {trade_date} — "
            f"another upload for this date committed first; returning its row instead "
            f"of creating a duplicate active row"
        )
        winner = await get_active(session, trade_date)
        if winner is None:
            # Vanishingly unlikely (the winner would have to have been
            # superseded again in between) — nothing sensible to return.
            raise
        if winner.content_hash != new_hash:
            logger.warning(
                f"Workflow upload: the row that won the race for {trade_date} has "
                f"DIFFERENT content than what this request tried to upload (hash "
                f"{winner.content_hash[:12]} vs {new_hash[:12]}) — re-upload if this "
                f"request's content was the intended one"
            )
        # is_new=False either way: this call did not create the active row,
        # regardless of whether its content happens to match.
        return winner, False

    logger.info(
        f"Workflow uploaded: id={new_row.id} date={trade_date} by={uploaded_by}"
    )
    return new_row, True


@otel_trace
async def get_history(
    session: AsyncSession,
    trade_date: date,
) -> list[EdpProperties]:
    """Return all config versions for a date (active + superseded), newest first."""
    stmt = (
        select(EdpProperties)
        .where(
            EdpProperties.trade_date == trade_date,
        )
        .order_by(EdpProperties.uploaded_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())
