# Mock CBOS Server

A standalone, self-contained simulator for the CBOS APIs used by the EDP
Billing segment execution flow. Use it to test the EDP agent end-to-end
without VPN/VDI access to the real MOFSL CBOS system.

## Design goals (per requirements)

1. **Fully isolated folder.** Nothing under `src/` imports from `mock_cbos/`,
   and `mock_cbos/` imports nothing from `src/`. Deleting this folder has
   **zero impact** on the agent.
2. **One-place cutover.** Going from "test against mock" to "test against
   real CBOS" only requires changing the CBOS URLs in your `.env` file —
   no code changes anywhere.

## What it implements

The 7-step segment execution flow — identical for all 7 segments (CASH/EQ,
F&O/DR, CD/CUR, SLBM/SL, MCX, NCDEX, MTF; MTF is not special-cased) — on a
single port (both real CBOS base URLs, `EDP Status API` port 8087 and `Main
Process API` port 8003, have distinct paths so one mock server process can
stand in for both):

| Step(s) | Endpoint | Purpose |
|---|---|---|
| 1, 3, 5, 6, 7 | `POST /api/edp/file_process_status` | Holiday check / FILEUPLOAD / BILLPOSTING / RECON / CONTRACTNOTEGENERATION GTG polls |
| 2 | `POST /v1/api/brokerage/getdropdown` | `EXISTINGPROCESSID` — check for an existing process ID before reserving |
| 2, 4 | `POST /v1/api/process/getNewTradeProcess` | Reserve PROCESSID (`PROCESSID:"0"`, only if not found via getdropdown) / execute (`PROCESSID:<actual>`) |
| — | Upload stubs | Kept for completeness — the EDP agent never calls these (RPA's job) |

Plus admin/control endpoints (`/mock/*`) to script test scenarios (holiday
simulation, forcing a stage to stay pending, forcing instant readiness,
changing how many polls are needed before `TRUE`).

## Running it

```bash
cd mock_cbos
pip install -r requirements.txt
python -m mock_cbos.main
```

Or, from the repo root:

```bash
pip install -r mock_cbos/requirements.txt
python -m mock_cbos.main
```

By default it listens on `http://0.0.0.0:9100`. Override with env vars:

```bash
MOCK_CBOS_PORT=9200 MOCK_CBOS_HOST=127.0.0.1 python -m mock_cbos.main
```

Interactive API docs: `http://localhost:9100/docs`

## Pointing the agent at it

In your `.env` file (repo root), set:

```dotenv
CBOS_STATUS_URL=http://localhost:9100
CBOS_PROCESS_URL=http://localhost:9100
CBOS_USE_MOCK=false
```

`CBOS_USE_MOCK=false` tells the agent's `CbosClient` to make real HTTP calls
(to this mock server) instead of using its own in-process fake responses.
Both URLs point at the same mock server since it serves both API groups.

**To switch to the real CBOS system later**, change only those two lines:

```dotenv
CBOS_STATUS_URL=http://10.167.202.234:8087
CBOS_PROCESS_URL=http://10.167.202.164:8003
CBOS_USE_MOCK=false
```

No agent code changes needed either way.

## Default behaviour

- Every GTG poll (`file_process_status`) returns `FALSE` for the first
  `ready_after` calls (default **2**), then `TRUE` from then on — simulating
  realistic polling. State is tracked per `(segment, process_name)` pair.
- `getNewTradeProcess` with `PROCESSID:"0"` reserves a new incrementing fake
  PID (starting at `17001`) per `(GROUPNAME, TRADEDATE)`.
- `getNewTradeProcess` with a real PROCESSID always returns success (`Table2`
  all `SUCCESS`).
- `getdropdown(EXISTINGPROCESSID)` only reports a process ID as "found" once
  it has actually been reserved via `getNewTradeProcess(PROCESSID="0")` for
  that `(segment, trade_date)` — before that it correctly returns an empty
  `Result`, exercising the "reserve a new one" branch of Step 2.

## Scripting test scenarios

```bash
# Reset all state between test runs
curl -X POST http://localhost:9100/mock/reset

# Make GTG checks pass after only 1 poll instead of 2 (faster tests)
curl -X POST http://localhost:9100/mock/config/ready_after \
     -H "Content-Type: application/json" -d '{"ready_after": 1}'

# Simulate a market holiday for EQ (BeginFileUpload -> SKIP)
curl -X POST http://localhost:9100/mock/scenario/holiday \
     -H "Content-Type: application/json" -d '{"segment": "EQ", "enabled": true}'

# Force FILEUPLOAD to stay stuck at FALSE for DR (test window-deadline TIMEOUT)
curl -X POST http://localhost:9100/mock/scenario/stuck \
     -H "Content-Type: application/json" \
     -d '{"segment": "DR", "process_name": "FILEUPLOAD", "enabled": true}'

# Force BILLPOSTING to be instantly ready for EQ (skip past a slow poll loop)
curl -X POST http://localhost:9100/mock/scenario/force_ready \
     -H "Content-Type: application/json" \
     -d '{"segment": "EQ", "process_name": "BILLPOSTING", "enabled": true}'

# Inspect current in-memory state (poll counts, reserved PIDs, overrides)
curl http://localhost:9100/mock/state
```

## Notes

- State is purely in-memory and resets whenever the server restarts.
- This server does not persist anything to disk and has no database.
- Safe to run alongside the agent on a different port — no conflicts.
