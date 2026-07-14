# ADR-0001: The repository uses a caller-owned session (Unit-of-Work), not its own

- Status: Accepted
- Date: 2026-07-14

## Context

An architecture review proposed making the EDP repository "own its session":
each `repository/*.py` function would open and commit its own transaction, and
an in-memory fake repository adapter would replace the real Postgres in tests.
Serialization and terminal-alert email would also be pulled out of the
repository so it dealt only in domain rows.

On inspection the current shape is deliberate, and the proposed change conflicts
with a load-bearing invariant.

**The transaction model is a Unit-of-Work owned by the orchestrator:**

- `database.get_session()` opens one session and commits on clean exit / rolls
  back on exception.
- `EdpOrchestrator._drive_unit()` opens **one session per unit-cycle** and
  threads it through the whole cycle: `get_one` → row mutations →
  `state_machine.execute_handler()` → `touch_heartbeat`. The cycle is one
  transaction.
- Inside `execute_handler`, the state machine issues `flush()` for intermediate
  writes and exactly one deliberate `commit()` — a **crash-safety marker** that
  forces the `TRIGGERING` state to disk *before* firing the irreversible CBOS
  trigger, so a crash mid-trigger cannot roll it back
  (`RealSegmentStateMachine.py` / `PostTradeStateMachine.py`; the comment there
  spells out "must be `commit()`, not `flush()`"). The double-trigger / crash
  recovery race tests depend on this.

If the repository owned per-call transactions, `get_one` / `move_to_state` /
heartbeat would each auto-commit independently, there would be no enclosing
cycle transaction, and the state machine could no longer place its pre-trigger
durability checkpoint. The invariant "one cycle = one controllable transaction
with a crash-safety commit before the irreversible external call" would be lost.

**The other two "leaks" are also deliberate:**

- **Terminal-alert email in `move_to_state`.** The alert fires here because this
  is the single place that knows "a row just crossed into a terminal status" —
  every terminal transition (state machine, orchestrator timeout, API
  retry/skip) routes through it. Moving the alert out would *scatter* the
  trigger across N callers, worsening locality. The only real smell — the
  persistence module importing the email service — is already test-isolated by
  the `no_real_emails` conftest fixture.
- **Serialization in `get_day_summary`.** Under the async engine, serializing a
  row after its session closes triggers a synchronous lazy-reload that crashes
  (documented at `repository/segment.py`'s `updated_at` comment). Serialization
  therefore wants to happen while the session is still open, which the in-repo
  placement guarantees.

## Decision

Keep the repository as a set of functions that accept the **caller's**
`AsyncSession`. The orchestrator owns the per-cycle transaction; the state
machine's pre-trigger `commit()` stays. Terminal-alert email stays in
`move_to_state`; row serialization stays inside the repository (executed while
the session is open).

Do **not** introduce an in-memory fake repository adapter. Tests run against a
real Postgres, isolated by unique far-future `trade_date`s and a module-global
engine monkeypatch (`conftest.py`), because the concurrency contract the tests
verify (`get_or_create` `IntegrityError` recovery, the unique partial index,
two-session duplicate-row races) lives in the database and cannot be faithfully
faked.

## Consequences

- The session appears in every repository signature. This is intentional: it is
  the mechanism that makes the transaction boundary controllable, not a leak.
- Tests require a reachable Postgres (see the project test-environment setup).
  There is no fast in-memory DB path, by design.
- Future architecture reviews should not re-propose "repository owns its
  session", extracting the terminal-alert email, or a fake repository adapter
  without first revisiting this ADR and the crash-safety invariant above.
