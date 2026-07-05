"""
Cross-session lock-acquisition race.

repository.acquire_lock() used to read row.lock_json in Python, branch on
it, then write the new state back via ORM attribute assignment + flush().
That is a check-then-act race: two DB sessions (two pod replicas briefly
overlapping during a rolling deploy, or two overlapping tasks in one
process) that both read the same UNLOCKED row before either commits would
BOTH conclude they'd acquired the lock and BOTH proceed to call CBOS —
defeating the entire point of the lock and reopening the exact
double-trigger risk the TRIGGERING mechanism (see
test_trigger_double_trigger_protection.py) is supposed to close.

acquire_lock() is now a single conditional `UPDATE ... WHERE lock_json
currently looks unlocked` — atomic at the database level via row-level
locking, so only one of two concurrently racing UPDATEs can ever actually
change the row.

This test proves it by firing two real, independent sessions at the same
row via asyncio.gather (true concurrency, not sequential calls) and
asserting exactly one wins, repeated several times to make a reintroduced
race very unlikely to go unnoticed.
"""

from __future__ import annotations

import asyncio

from src.agent.edp import repository
from src.agent.edp.utils.locking import lock_owner, lock_state

from . import helpers

SEGMENT = "CUR"


async def _try_acquire(session_factory, test_date, owner: str) -> bool:
    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        acquired = await repository.acquire_lock(session, row, owner)
        await session.commit()
        return acquired


async def test_concurrent_acquire_lock_has_exactly_one_winner(cfg, session_factory, test_date):
    await helpers.seed_day(session_factory, test_date, cfg)

    results = await asyncio.gather(
        _try_acquire(session_factory, test_date, "pod-A"),
        _try_acquire(session_factory, test_date, "pod-B"),
    )

    assert sorted(results) == [False, True], (
        f"expected exactly one winner and one loser, got {results} — "
        "both True means the lock race is NOT closed (double-trigger risk); "
        "both False means acquire_lock is broken outright"
    )

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert lock_state(row) == "LOCKED"
    assert lock_owner(row) in ("pod-A", "pod-B")


async def test_concurrent_acquire_lock_many_racers_exactly_one_winner(cfg, session_factory, test_date):
    """Same race with more contenders (5), to make a flaky/reintroduced race
    much less likely to slip through by chance."""
    await helpers.seed_day(session_factory, test_date, cfg)

    owners = [f"pod-{i}" for i in range(5)]
    results = await asyncio.gather(
        *(_try_acquire(session_factory, test_date, owner) for owner in owners)
    )

    assert results.count(True) == 1, f"expected exactly 1 winner among 5 racers, got {results}"
    assert results.count(False) == 4

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
    assert lock_state(row) == "LOCKED"
    assert lock_owner(row) in owners
