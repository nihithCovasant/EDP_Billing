# EDP Billing test suite

Integration tests for the EDP orchestrator + pipeline, run against the **same
database** the agent itself uses (`DATABASE_URL` / `DB_*` env vars from `.env`
— see `src/agent/edp/config.py::load_edp_config`). Nothing is mocked at the
database layer; only the CBOS HTTP calls are (via `src/tools/cbos_client.py`'s
own `use_mock=True` in-process mock, plus `tests/fakes.py::FailingCbosClient`
for failure injection). No network calls are made and `mock_cbos/` does not
need to be running.

## Running

```powershell
.venv\Scripts\python.exe -m pytest tests\ -v
```

(`pytest` + `pytest-asyncio` are in `requirements.txt`.)

## Why these tests are safe to run against a live/shared database

Every test gets a unique, **far-future** `trade_date` (5000-55000 days out,
see the `test_date` fixture in `conftest.py`), and cleans up its own
`segment_execution` / `edp_properties` rows before and after. This means:

- Tests never touch real trading data.
- Tests never collide with a live agent instance's wake loop (`loop.py`),
  which only ever resolves **today's** real `active_date` — never a
  synthetic date thousands of days out.
- Tests can run concurrently with `python -m src.agent` in another terminal
  without interfering with it.

## Why tests don't call `orchestrator.run_wake_cycle()` directly

`run_wake_cycle()` always resolves `active_date` from the real wall clock
(`resolve_active_date(datetime.now(...), cutoff_hour, tz)`) — it has no way to
target an arbitrary test date. `tests/helpers.py` instead replicates the same
"iterate segments in sequence, halt on FAILED" loop body that
`run_wake_cycle()` uses, but drives `orchestrator._process_one_segment()`
directly against a caller-supplied `trade_date`. Everything below the
per-segment level — locking (`lock_json`), the 7-stage pipeline, CBOS calls,
and the halt-on-FAILED sequencing rule — is exercised exactly as in
production; only the wall-clock scheduling glue is bypassed.

## Files

| File | Purpose |
|---|---|
| `conftest.py` | DB engine/session fixtures, `test_date` (isolated far-future date), and wiring so `orchestrator._process_one_segment()`'s internal `database.get_session()` calls route to the test's own engine. |
| `fakes.py` | `FailingCbosClient` — behaves like the normal CBOS mock except for one `(segment, process_name)` pair, which always returns a permanent (non-transient) error. |
| `helpers.py` | `seed_day`, `drive_until_terminal`, `run_one_cycle`, `cleanup_day` — the test harness described above. |
| `test_day1_all_segments_success.py` | **Scenario 1**: all 8 real segments + the virtual `MTFOPS` chain complete successfully for one trading day. Also checks fixed sequence ordering and that the day-summary/serializer API output has no leftover `domain`/`window_*_at` fields. |
| `test_day2_segment_process_failure.py` | **Scenario 2**: `EQ`'s 2nd process (`BILLPOSTING`) returns a permanent CBOS error → `EQ` ends `FAILED`, every segment after it (+ `MTFOPS`) stays `PENDING` (chain halts), and a manual `retry_segment` + healthy CBOS client lets the day finish. |

## Segment process ordering (for "Nth process failed" scenarios)

Per `models.py::EdpProperties.workflow_json` docs, each segment's internal
process order is: `fileupload` (1) → `BillPost` (2) → `Reconn` (3) →
`ContractNote` (4). "2nd process failed" in `test_day2_segment_process_failure.py`
means the `BILLPOSTING` CBOS poll (`pipeline/stages.py::handle_await_billposting`).
