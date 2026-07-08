"""
JSON dict columns (processes_json, workflow_json) are wrapped with
MutableDict.as_mutable(JSON) so an in-place mutation
(row.processes_json["x"] = y) is tracked and flushed correctly, not just
the "reassign the whole dict" convention json_helpers.py uses. This test
proves that: mutate in place, flush, reload in a new session, see the change.
"""

from __future__ import annotations

from src.agent.edp import repository
from src.agent.edp.models import EdpProperties

from . import helpers


async def test_in_place_mutation_of_processes_json_is_persisted(cfg, session_factory, test_date):
    await helpers.seed_day(session_factory, test_date, cfg)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "CUR")
        # Deliberately NOT the json_helpers.py convention (no reassignment
        # of the whole dict) — a raw in-place mutation.
        row.processes_json["probe"] = {"touched": True}
        await session.commit()

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "CUR")
    assert row.processes_json.get("probe") == {"touched": True}, (
        "in-place mutation of processes_json was lost — MutableDict wrapping is missing/broken"
    )


async def test_in_place_mutation_of_workflow_json_is_persisted(cfg, session_factory, test_date):
    await helpers.seed_day(session_factory, test_date, cfg)

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
        workflow.workflow_json["probe"] = "touched"
        await session.commit()

    async with session_factory() as session:
        workflow = await repository.get_active(session, test_date)
    assert workflow.workflow_json.get("probe") == "touched"
