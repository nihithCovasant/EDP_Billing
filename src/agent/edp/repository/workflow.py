"""
edpb_properties table — daily config upload and retrieval.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EdpProperties
from ..utils.datetime_utils import now_ist
from cams_otel_lib import Logger as logger, otel_trace


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
    Return the most recently uploaded active config on or before `as_of_date`
    — carries the last uploaded config forward until a newer one exists.
    Fallback used when get_active(as_of_date) finds nothing for today.
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
    Insert the workflow config for the day as a brand-new row, superseding
    whatever was active before. No content-hash dedup — every call creates
    a new audit row. Returns (row, is_new); is_new is False only if this
    call lost a concurrent-upload race (see IntegrityError handling below).

    Concurrency: check-then-act (get_active() then insert), but a unique
    partial index (one active row per trade_date) makes the write atomic
    at the DB level — the losing concurrent INSERT raises IntegrityError
    instead of leaving two is_active=True rows.
    """
    existing = await get_active(session, trade_date)

    ts = now_ist()
    if existing:
        existing.is_active = False
        existing.superseded_at = ts
        logger.info(f"Workflow superseded: id={existing.id} for {trade_date}")

    new_row = EdpProperties(
        trade_date=trade_date,
        workflow_json=workflow_json,
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
        # is_new=False: this call did not create the active row.
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
