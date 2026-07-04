"""
edp_properties table — daily config upload and retrieval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
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
    await session.flush()
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
