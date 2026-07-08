"""
Concurrent workflow uploads for the same trade_date.

repository.workflow.upload() is check-then-act: get_active() then
conditionally insert. Without a database-level constraint, two concurrent
uploads for the same date (a manual re-upload racing an automated retry)
could both pass the check before either commits, leaving two
is_active=True rows for the same trade_date — which then breaks
get_active()'s scalar_one_or_none() on every future read with
MultipleResultsFound.

A unique partial index (one active row per trade_date, see
models.EdpProperties.__table_args__) now makes the underlying write atomic
at the database level; upload() catches the loser's IntegrityError and
returns the winning row instead of raising or corrupting the table.

Two tests, at two levels:
  1. A low-level test that drives two real INSERTs with explicit
     synchronization (one txn flushed-but-uncommitted while the other
     attempts its own INSERT), proving the unique partial index itself
     forces Postgres to serialize them — the second INSERT must block
     until the first resolves, then fails.
  2. A test exercising upload()'s own IntegrityError handling end-to-end,
     using a monkeypatch to deterministically interleave "another upload
     commits in between this call's get_active() and its own insert" —
     the exact window a database-level constraint (not just application
     logic) is needed to close, since it can't be reproduced reliably via
     plain asyncio.gather() on two fast local round-trips.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

from src.agent.edp import repository
import src.agent.edp.repository.workflow as workflow_repo
from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.models import EdpProperties

from . import helpers


def _workflow_variant(cfg, login_id: str) -> dict:
    segments = [
        {
            "segment_code": code,
            "login_id": login_id,
            "window_start": "00:00",
            "window_end": "23:59",
        }
        for code in helpers.SEGMENT_ORDER
    ]
    return build_default_workflow_json(segments)


async def test_unique_index_serializes_concurrent_inserts_for_same_date(cfg, session_factory, test_date):
    workflow_json_a = _workflow_variant(cfg, "RACER_A")
    workflow_json_b = _workflow_variant(cfg, "RACER_B")
    a_flushed = asyncio.Event()

    async def racer_a() -> bool:
        async with session_factory() as session:
            row = EdpProperties(
                trade_date=test_date,
                workflow_json=workflow_json_a,
                is_active=True,
                uploaded_by="racer-a",
            )
            session.add(row)
            await session.flush()  # INSERT issued, uncommitted — takes the index's lock
            a_flushed.set()
            await asyncio.sleep(0.3)  # hold the txn open while B tries to insert
            await session.commit()
            return True

    async def racer_b() -> bool:
        await a_flushed.wait()
        async with session_factory() as session:
            row = EdpProperties(
                trade_date=test_date,
                workflow_json=workflow_json_b,
                is_active=True,
                uploaded_by="racer-b",
            )
            session.add(row)
            try:
                await session.flush()
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    a_ok, b_ok = await asyncio.gather(racer_a(), racer_b())
    assert a_ok is True
    assert b_ok is False, (
        "B's INSERT must be rejected by the unique partial index while A's "
        "transaction is still open — if this is True, the race isn't closed"
    )

    async with session_factory() as session:
        active = await repository.get_active(session, test_date)  # raises
        # MultipleResultsFound instead of returning a single row if broken.
    assert active is not None
    assert active.uploaded_by == "racer-a"


async def test_upload_handles_lost_race_via_integrity_error(cfg, session_factory, test_date):
    """
    Simulates "another pod's upload commits in the window between this
    call's get_active() and its own insert" by monkeypatching get_active()
    to sneak in a real, separate, committed upload as a side effect —
    deterministic, unlike relying on asyncio.gather() timing for two fast
    local round-trips to happen to overlap.
    """
    workflow_json_winner = _workflow_variant(cfg, "WINNER")
    workflow_json_loser = _workflow_variant(cfg, "LOSER")
    real_get_active = workflow_repo.get_active
    already_sneaked_in = False

    async def get_active_then_sneak_in_a_committer(session, trade_date):
        nonlocal already_sneaked_in
        result = await real_get_active(session, trade_date)
        if not already_sneaked_in:
            already_sneaked_in = True
            # Insert the "other pod's" winning row directly (bypassing
            # upload()) so this doesn't recurse back through the patched
            # get_active() a second time.
            async with session_factory() as other_session:
                other_session.add(EdpProperties(
                    trade_date=trade_date,
                    workflow_json=workflow_json_winner,
                    is_active=True,
                    uploaded_by="winner",
                ))
                await other_session.commit()
        return result

    async with session_factory() as session:
        with patch.object(workflow_repo, "get_active", side_effect=get_active_then_sneak_in_a_committer):
            row, is_new = await workflow_repo.upload(
                session, test_date, workflow_json_loser, uploaded_by="loser",
            )
        await session.commit()

    assert is_new is False, "the call that lost the race must not report itself as having created a new row"
    assert row.uploaded_by == "winner", "must return the row that actually won, not raise or return nothing"

    async with session_factory() as verify_session:
        active = await repository.get_active(verify_session, test_date)  # raises
        # MultipleResultsFound instead of returning a single row if broken.
    assert active is not None
    assert active.uploaded_by == "winner"
