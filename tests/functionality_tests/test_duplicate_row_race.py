"""
Bug-hunting tests against repository/segment.py.

1. get_or_create() concurrent duplicate-insert race — SegmentExecution has
   a DB-level UniqueConstraint("trade_date", "segment_code") (models.py),
   but unlike repository.workflow.upload() (see test_workflow_upload_race.py,
   which explicitly catches the loser's IntegrityError and re-fetches the
   winning row), get_or_create() has no try/except around its INSERT/flush.
   Two callers that both pass their own get_one() "not found" check before
   either commits will both attempt an INSERT for the same
   (trade_date, segment_code) — only one can win the unique constraint; the
   loser's flush should raise an unhandled IntegrityError.

2. move_to_state()'s best-effort alert email — _send_terminal_alert() wraps
   send_segment_alert() in try/except and only logs on failure. Confirm
   that an email-service outage never propagates out of move_to_state().

3. move_to_state() must commit the terminal-status write BEFORE sending the
   alert email, not after (i.e. not just flush() and rely on the caller's
   own later commit). Sending first was a real double-alert bug: if the
   caller's own subsequent commit then failed for any reason (a transient
   DB blip/deadlock/timeout), the whole transaction rolled back — undoing
   the transition — but the email had already gone out. The next wake
   cycle would reprocess the still-non-terminal row, reach the same
   terminal outcome again, and re-send the same alert a second time.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.exc import IntegrityError

from src.agent.edp import repository
from src.agent.edp.config import build_default_workflow_json
from src.agent.edp.models import SegmentStatus


async def test_get_or_create_concurrent_duplicate_insert_race(cfg, session_factory, test_date):
    """
    Two separate sessions both call get_or_create() for the same
    (test_date, "EQ"), synchronized via asyncio.Event so both pass their own
    get_one()-finds-nothing check before either flushes/commits.
    """
    workflow_json = build_default_workflow_json(
        [
            {
                "segment_code": "EQ",
                "login_id": "CV0001",
                "window_start": "00:00",
                "window_end": "23:59",
            }
        ]
    )
    async with session_factory() as session:
        _workflow, _ = await repository.upload(session, test_date, workflow_json, uploaded_by="test")
        await session.commit()

    a_checked = asyncio.Event()
    b_checked = asyncio.Event()

    async def racer_a():
        async with session_factory() as session:
            workflow_a = await repository.get_active(session, test_date)
            # Mirror get_or_create()'s own get_one() check manually so we can
            # inject the synchronization point exactly where the real
            # implementation's SELECT-then-INSERT window is: after both
            # racers have confirmed "no row exists yet", before either flushes.
            existing = await repository.get_one(session, test_date, "EQ")
            assert existing is None
            a_checked.set()
            await b_checked.wait()  # hold here until B has also seen "not found"

            result = await repository.get_or_create(session, workflow_a, test_date, "EQ")
            # A real caller always commits after get_or_create() — without a
            # commit here, neither racer's INSERT is ever finalized, so
            # Postgres's unique index has nothing committed to conflict
            # against and both flushes spuriously "succeed". Match production.
            await session.commit()
            return result

    async def racer_b():
        async with session_factory() as session:
            workflow_b = await repository.get_active(session, test_date)
            existing = await repository.get_one(session, test_date, "EQ")
            assert existing is None
            b_checked.set()
            await a_checked.wait()

            result = await repository.get_or_create(session, workflow_b, test_date, "EQ")
            await session.commit()
            return result

    results = await asyncio.gather(racer_a(), racer_b(), return_exceptions=True)

    exceptions = [r for r in results if isinstance(r, BaseException)]
    clean_rows = [r for r in results if not isinstance(r, BaseException)]

    if exceptions:
        # BUG CONFIRMED: get_or_create() does not catch IntegrityError on a
        # concurrent duplicate insert — the loser's flush raises instead of
        # gracefully returning the winner's row.
        assert len(exceptions) == 1, f"expected exactly one loser to raise, got {len(exceptions)}: {exceptions}"
        assert isinstance(exceptions[0], IntegrityError), (
            f"expected the unhandled exception to be an IntegrityError (from the unique "
            f"constraint), got {type(exceptions[0])}: {exceptions[0]}"
        )
        assert len(clean_rows) == 1
        print(
            "\n[FINDING CONFIRMED] get_or_create() does NOT catch IntegrityError on a "
            "concurrent duplicate insert for the same (trade_date, segment_code) — the "
            "loser's session raised instead of gracefully returning the winner's row."
        )
    else:
        # Bug NOT present: both calls returned cleanly, meaning get_or_create()
        # (or something wrapping it) already handles the race gracefully.
        assert len(clean_rows) == 2
        assert clean_rows[0].id == clean_rows[1].id, (
            "both racers returned a row, but they don't refer to the same row — "
            "still evidence of a duplicate-row problem"
        )
        print(
            "\n[FINDING: NOT PRESENT] Both concurrent get_or_create() calls returned "
            "cleanly with the same row — no unhandled IntegrityError occurred."
        )

    # Regardless of outcome: confirm no duplicate row was actually persisted.
    async with session_factory() as verify_session:
        rows = await repository.get_all_for_date(verify_session, test_date)
    eq_rows = [r for r in rows if r.segment_code == "EQ"]
    assert len(eq_rows) == 1, (
        f"expected exactly 1 row for (trade_date, 'EQ'), found {len(eq_rows)} — "
        f"the unique constraint should make this impossible regardless of get_or_create()'s "
        f"error handling"
    )


async def test_move_to_state_swallows_alert_email_failure(session_factory, test_date, monkeypatch):
    """
    move_to_state() must not raise even if the best-effort terminal alert
    email fails — an email outage must never break the actual pipeline.
    """
    import global_email_service

    def _raise_smtp_down(payload):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(global_email_service, "send_segment_alert", _raise_smtp_down)

    workflow_json = build_default_workflow_json(
        [
            {
                "segment_code": "EQ",
                "login_id": "CV0001",
                "window_start": "00:00",
                "window_end": "23:59",
            }
        ]
    )
    async with session_factory() as session:
        workflow, _ = await repository.upload(session, test_date, workflow_json, uploaded_by="test")
        await session.commit()

        row = await repository.get_or_create(session, workflow, test_date, "EQ")
        await session.commit()

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "EQ")
        try:
            await repository.move_to_state(session, row, SegmentStatus.FAILED, category="TEST", reason="test")
            await session.commit()
        except Exception as exc:
            raise AssertionError(
                "[NEW SERIOUS FINDING] move_to_state() raised even though the failing "
                f"alert email should be best-effort/swallowed: {type(exc).__name__}: {exc}"
            ) from exc

    async with session_factory() as verify_session:
        verified_row = await repository.get_one(verify_session, test_date, "EQ")
    assert verified_row.segment_status == SegmentStatus.FAILED, (
        "the state transition itself must have gone through despite the alert email failing"
    )
    print(
        "\n[FINDING CONFIRMED] move_to_state() swallows alert-email failures — the "
        "RuntimeError raised by send_segment_alert did not propagate; the FAILED "
        "transition committed successfully."
    )


async def test_move_to_state_commits_terminal_status_before_sending_alert(
    session_factory,
    test_date,
    monkeypatch,
):
    """
    Regression test for the double-alert bug: on a genuine terminal
    transition, move_to_state() must commit the DB write before calling
    send_segment_alert() — never the other way around. If the alert fires
    first (or before a durable commit), a later commit failure would undo
    the transition while the email has already gone out, causing a
    duplicate alert on the next cycle's retry.
    """
    import global_email_service

    call_order: list[str] = []

    def _record_alert(payload):
        call_order.append("alert_sent")

    monkeypatch.setattr(global_email_service, "send_segment_alert", _record_alert)

    workflow_json = build_default_workflow_json(
        [
            {
                "segment_code": "EQ",
                "login_id": "CV0001",
                "window_start": "00:00",
                "window_end": "23:59",
            }
        ]
    )
    async with session_factory() as session:
        workflow, _ = await repository.upload(session, test_date, workflow_json, uploaded_by="test")
        await session.commit()

        row = await repository.get_or_create(session, workflow, test_date, "EQ")
        await session.commit()

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, "EQ")

        real_commit = session.commit

        async def _tracked_commit():
            await real_commit()
            call_order.append("committed")

        monkeypatch.setattr(session, "commit", _tracked_commit)

        await repository.move_to_state(
            session,
            row,
            SegmentStatus.COMPLETED,
            now=None,
        )

    assert call_order == ["committed", "alert_sent"], (
        f"move_to_state() must commit the terminal transition BEFORE sending the "
        f"alert email — got order {call_order}. Sending the alert first (or before "
        f"a durable commit) is exactly the bug that caused duplicate alerts: a "
        f"later commit failure rolls back the transition after the email already "
        f"went out, so the next cycle re-processes and re-sends it."
    )

    async with session_factory() as verify_session:
        verified_row = await repository.get_one(verify_session, test_date, "EQ")
    assert verified_row.segment_status == SegmentStatus.COMPLETED, (
        "the COMPLETED transition must have actually been durably committed "
        "(not just flushed) by move_to_state() itself"
    )
