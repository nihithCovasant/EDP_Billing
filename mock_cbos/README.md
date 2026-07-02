# Mock CBOS Server

A standalone, self-contained simulator for every CBOS API referenced in
`EDP_Trade_Process_API_v2.docx`. Use it to test the EDP agent end-to-end
without VPN/VDI access to the real MOFSL CBOS system.

## Design goals (per requirements)

1. **Fully isolated folder.** Nothing under `src/` imports from `mock_cbos/`,
   and `mock_cbos/` imports nothing from `src/`. Deleting this folder has
   **zero impact** on the agent.
2. **One-place cutover.** Going from "test against mock" to "test against
   real CBOS" only requires changing the CBOS URLs in your `.env` file —
   no code changes anywhere.

## What it implements

All endpoints from the API doc, on a single port (they all have distinct
paths, so one mock server process can stand in for both real CBOS base
URLs — `EDP Status API` port 8087 and `Main Process API` port 8003):

| Step(s) | Endpoint | Purpose |
|---|---|---|
| 1, 7, 9, 10, 11 | `POST /api/edp/file_process_status` | Holiday check / FILEUPLOAD / BILLPOSTING / RECON / CONTRACTNOTEGENERATION GTG polls |
| 2, 8 | `POST /v1/api/process/getNewTradeProcess` | Reserve PROCESSID (`PROCESSID:"0"`) / execute (`PROCESSID:<actual>`) |
| 5 | `POST /v1/api/brokerage/getdropdown` | `EXISTINGPROCESSID` crash-recovery lookup |
| 12, 14, 16, 18, 21, 23, 25 | `POST /api/edp/file_process_status` | MTF chain GTG polls (`CollateralValuation`, `CollateralAllocation`, `FundTransfer`, `EARLYPAYIN`, `WEEKLYAUTOCLOSURE`, and `BILLPOSTING` re-checks) |
| 13 | `POST /v1/api/process/GetCollateralValuation` | Collateral Valuation trigger |
| 15 | `POST /v1/api/process/MTFTradeProcessCollateralAllocation` | Collateral Allocation trigger |
| 17 | `POST /v1/api/process/MTFTradeProcessFundTransfer` | Fund Transfer trigger |
| 19, 20, 22, 24 | `POST /v1/api/process/MTFTradeProcess` | MTF Buy/Sell/Weekly Auto Closure (`TYPE` field distinguishes) |
| 3, 4, 6 | Upload stubs | Kept for completeness — the EDP agent never calls these (RPA's job) |
| 26 | *not implemented* | Requires manual Ops file drops — out of scope |

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
- All MTF trigger endpoints (`GetCollateralValuation`,
  `MTFTradeProcessCollateralAllocation`, `MTFTradeProcessFundTransfer`,
  `MTFTradeProcess`) always return a success message immediately.

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

# Replay the documented "@job_name ('MTF_RISK_UPDATE') does not exist" quirk
# for MTFTradeProcessFundTransfer (step 17) — still HTTP 200/Success, so the
# agent should treat it as a fired trigger and advance anyway
curl -X POST http://localhost:9100/mock/scenario/fund_transfer_quirk \
     -H "Content-Type: application/json" -d '{"enabled": true}'
```

## Notes

- State is purely in-memory and resets whenever the server restarts.
- This server does not persist anything to disk and has no database.
- Safe to run alongside the agent on a different port — no conflicts.
