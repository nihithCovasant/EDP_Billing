"""
edpb_properties table — daily config upload and retrieval.
"""

from __future__ import annotations

from datetime import date

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EdpProperties
from ..utils.datetime_utils import now_ist


@otel_trace
async def get_active(
    session: AsyncSession,
    trade_date: date,
) -> EdpProperties | None:
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
) -> EdpProperties | None:
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
    version_name: str | None = None,
    overwrite_version: bool = False,
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

    version_name: optional label to attach to the new row. If the name is
    already owned by another row, this raises ValueError unless
    overwrite_version=True, in which case the name is moved here (cleared
    off the old owner) — see move_version_name(). Callers (the API layer)
    should translate that ValueError into a 409, not a 422/500.
    """
    if version_name and not overwrite_version:
        existing_owner = await get_by_version_name(session, version_name)
        if existing_owner is not None:
            raise ValueError(
                f"version_name {version_name!r} already exists (id={existing_owner.id}). "
                f"Please reupload with a different version_name, or pass "
                f"overwrite_version=true to replace the existing one."
            )

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

    if version_name:
        await move_version_name(session, version_name, new_row)

    logger.info(f"Workflow uploaded: id={new_row.id} date={trade_date} by={uploaded_by} version_name={version_name!r}")
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


# =============================================================================
# Named versions — a version_name is an independent label on any row,
# unrelated to trade_date/is_active. At most one row owns a given name at a
# time (case-insensitive partial unique index in models.py), so "apply" and
# "save with overwrite" always MOVE the name rather than duplicate it.
# =============================================================================


@otel_trace
async def get_by_version_name(
    session: AsyncSession,
    version_name: str,
) -> EdpProperties | None:
    """Return the single row that currently owns this name, or None."""
    stmt = select(EdpProperties).where(func.lower(EdpProperties.version_name) == version_name.lower())
    return (await session.execute(stmt)).scalar_one_or_none()


@otel_trace
async def list_versions(session: AsyncSession) -> list[EdpProperties]:
    """All named rows (version_name IS NOT NULL), most recently uploaded first."""
    stmt = (
        select(EdpProperties).where(EdpProperties.version_name.is_not(None)).order_by(EdpProperties.uploaded_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


@otel_trace
async def move_version_name(
    session: AsyncSession,
    version_name: str,
    target_row: EdpProperties,
) -> None:
    """
    Attach `version_name` to `target_row`, first clearing it off whichever
    row currently owns it (if any, and if that's not `target_row` itself).
    Caller commits/flushes. Raises IntegrityError (via flush) if two
    concurrent calls race for the same name — same pattern as upload().
    """
    current_owner = await get_by_version_name(session, version_name)
    if current_owner is not None and current_owner.id != target_row.id:
        current_owner.version_name = None
        await session.flush()
    target_row.version_name = version_name
    await session.flush()


@otel_trace
async def clear_version_name(session: AsyncSession, version_name: str) -> bool:
    """
    Detach a name from whichever row owns it ("soft delete" of the name —
    the row/config itself is untouched, just no longer reachable by name).
    Returns False if no row currently owns this name.
    """
    owner = await get_by_version_name(session, version_name)
    if owner is None:
        return False
    owner.version_name = None
    await session.flush()
    return True
