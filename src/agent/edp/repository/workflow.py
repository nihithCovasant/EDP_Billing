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
    Insert the workflow config for the day as a brand-new row.

    There is no content-hash dedup check: every call to this function
    creates a new row and marks it active, superseding whatever was active
    before for this trade_date (if anything). Re-uploading identical JSON
    on purpose or by mistake still creates a new audit row — the caller is
    always trusted to know it wants a new version. Returns (row, is_new)
    with is_new always True on success, kept for API/response-shape
    backward compatibility; it is only False when this call lost a
    concurrent-upload race (see below) and ended up not creating anything.

    Concurrency: this is check-then-act (get_active() then insert), but a
    unique partial index — one active row per trade_date, see
    models.EdpProperties.__table_args__ — makes the actual write atomic at
    the database level. Two concurrent uploads for the same date (a manual
    re-upload racing an automated retry) can both pass the get_active()
    check before either commits, but only one INSERT can ever land; the
    other raises IntegrityError, caught below and resolved by returning
    whichever row actually won instead of crashing the request or leaving
    two is_active=True rows (which would break every future get_active()
    call with MultipleResultsFound).
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
