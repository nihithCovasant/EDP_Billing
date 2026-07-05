# EDP Billing test suite

Integration tests for the EDP orchestrator + pipeline, run against the **same
database** the agent itself uses (`DATABASE_URL` / `DB_*` env vars from `.env`
— see `src/agent/edp/config.py::load_edp_config`). Nothing is mocked at the
database layer; only the CBOS HTTP calls are (via `src/tools/cbos_client.py`'s
own `use_mock=True` in-process mock, plus `tests/fakes.py::FailingCbosClient`
/ `TransientTriggerFailureCbosClient` for failure injection). No network
calls are made and `mock_cbos/` does not need to be running.

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
| `fakes.py` | `FailingCbosClient` — behaves like the normal CBOS mock except for one `(segment, process_name)` pair, which always returns a permanent (non-transient) error. `TransientTriggerFailureCbosClient` — fails only the first trigger-mode `getNewTradeProcess` call for one segment with a transient error, then behaves normally. `CountingPostTradeTriggerCbosClient` — counts every real call to a post-trade trigger endpoint, used to prove a resumed post-trade trigger never fires twice. |
| `helpers.py` | `seed_day`, `drive_until_terminal`, `run_one_cycle`, `cleanup_day` — the test harness described above, plus `seed_post_trade_day`, `drive_post_trade_until_terminal`, `run_one_post_trade_cycle`, `fixed_post_trade_now_for` for the T+1 post-trade chain (driven via `orchestrator._process_one_post_trade()`, independent of the segment harness). |
| `test_day1_all_segments_success.py` | **Scenario 1**: all 7 segments (`EQ, DR, CUR, SL, MCX, NCDEX, MTF` — `MTF` is a normal segment, not special-cased) complete successfully for one trading day. Also checks fixed sequence ordering, Step 2's get-or-reserve behavior, and that the day-summary/serializer API output has no leftover `domain`/`window_*_at` fields. |
| `test_day2_segment_process_failure.py` | **Scenario 2**: `EQ`'s 2nd process (`BILLPOSTING`) returns a permanent CBOS error → `EQ` ends `FAILED`, every segment after it (including `MTF`) stays `PENDING` (chain halts), and a manual `retry_segment` + healthy CBOS client lets the day finish. |
| `test_post_trade_processes.py` | **T+1 post-trade chain**: all 5 processes (`COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT`) complete successfully — run entirely independently of the 7 segments (none are even seeded); Collateral Allocation failing halts the remaining 3 (stay `PENDING`); Process 1's 02:30 IST window gate blocks/unblocks correctly. Also: a post-trade trigger resumed with an unconfirmed `"TRIGGERING"` marker (no CBOS-side status check exists for post-trade, unlike real segments) marks the process `FAILED` with an explicit "needs manual verification" reason and **never** calls the trigger endpoint again; a genuinely stale lock at `TRIGGER_JOB`/`"TRIGGERING"` is unlocked (not silently `SKIPPED`) by `recover_stale_locks()`, mirroring the real-segment exception. |
| `test_trigger_double_trigger_protection.py` | **Pod-failure / double-trigger prevention**: proves the `TRIGGERING` pre-commit marker + `pipeline.stages._recover_trigger()` decision tree — (1) pod died before CBOS ever got the trigger → recovery check sees all-`PENDING`/empty `Table2` and safely re-fires it (2 total CBOS calls: check + trigger); (2) pod died after CBOS already started it → recovery check sees an `IN_PROGRESS` step and does **not** re-fire (2 total calls: check only, no second trigger); (3) end-to-end transient network error on the real trigger call leaves `processes_json["trigger"]["status"]` as `"TRIGGERING"` (never `"FAILED"`) and self-heals on the next cycle via the same decision tree; (4) a genuinely stale (expired, never-released) lock on a `TRIGGERING` segment is **unlocked and resumed** by `repository.recover_stale_locks()` rather than marked `SKIPPED` — the one deliberate exception to the "skip on restart" policy, since it's the only way an unconfirmed trigger can safely reach the CBOS-checked recovery path above instead of being silently abandoned; (5) regression guard proving a stale lock at any *other* phase (e.g. `AWAIT_FILE_UPLOAD`) is still marked `SKIPPED` (`AGENT_RESTART`) exactly as before. |
| `test_lock_race.py` | **Cross-pod lock acquisition race**: two (or five) genuinely concurrent sessions (`asyncio.gather`) racing `repository.acquire_lock()` on the same row always produce exactly one winner — proves `acquire_lock()`'s atomic `UPDATE ... WHERE lock currently unlocked` closes the read-then-write race a plain "read state in Python, then write" implementation would have. |
| `test_workflow_upload_race.py` | **Config-upload duplicate-active-row race**: a unique partial index (one active `edp_properties` row per `trade_date`) forces Postgres to serialize two concurrent INSERTs for the same date — one test drives the raw INSERT race directly (explicit `asyncio.Event` synchronization, since two fast local round-trips otherwise rarely overlap by chance); the other exercises `repository.workflow.upload()`'s own `IntegrityError` handling end-to-end via a deterministic monkeypatch, proving it returns the winning row instead of raising or leaving two active rows (which would break `get_active()` with `MultipleResultsFound`). |
| `test_unmapped_phase_failure.py` | **Unmapped pipeline phase**: driving a real segment through the 7-step pipeline while it's sitting at a phase that only exists in the post-trade 3-step pipeline (`AWAIT_GTG`) proves `executor.advance_pipeline()` now durably marks the row `FAILED` (`SYSTEM_ERROR`) instead of just logging and leaving it `IN_PROGRESS` to be silently retried forever. |
| `test_config_loading.py` | **Fail loudly on bad config**: `load_edp_config()` logs a visible warning (not silence) whenever a critical setting (CBOS URLs/mock flag, database URL) falls through to a hardcoded default with no env var or `agent_config.json` value; `EDP_STRICT_CONFIG=true` turns that into a `RuntimeError` at startup instead of running silently misconfigured. |
| `test_liveness_probe.py` | **Liveness probe detects a wedged wake loop**: `EdpWakeLoop.liveness_check()` reports not-alive once a cycle has been "running" far longer than any reasonable multiple of `wake_interval_seconds` (simulated via `time.monotonic()` manipulation, no real hang needed); `HealthChecker.register_liveness_check()`/`is_alive()` wiring is proven end-to-end so `/health/live` would actually fail, letting Kubernetes restart a stalled pod instead of the old hardcoded `return True`. |
| `test_mutable_json_columns.py` | **JSON columns are `MutableDict`-wrapped**: an in-place mutation (`row.processes_json["x"] = y`, not the whole-dict-reassignment convention `json_helpers.py`/`locking.py` use) is now tracked and actually persisted across a fresh session reload, for `processes_json`, `lock_json`, and `workflow_json`. |

## Segment process ordering (for "Nth process failed" scenarios)

Per `models.py::EdpProperties.workflow_json` docs, each segment's internal
process order is: `fileupload` (1) → `BillPost` (2) → `Reconn` (3) →
`ContractNote` (4). "2nd process failed" in `test_day2_segment_process_failure.py`
means the `BILLPOSTING` CBOS poll (`pipeline/stages.py::handle_await_billposting`).
