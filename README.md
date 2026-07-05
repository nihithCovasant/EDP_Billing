# File Upload Handler

A FastAPI service that watches a segment/date folder tree for trade files,
uploads each one to CBOS, and tracks the outcome in PostgreSQL. Runs fully
automatically: a scheduler scans on an interval, discovered files are queued,
and a single background worker uploads them one at a time.

```
Server Start
  -> Initialize DB
  -> Start Queue Worker
  -> Start Scheduler
  -> Begin File Processing (automatic, no manual steps)
```

---

## Architecture

```
FastAPI (app.main)
  |
  |-- Scheduler (APScheduler, runs every POLL_INTERVAL_MINUTES)
  |     -> discovers files, enqueues them
  |     -> never uploads, never touches the database directly
  |
  |-- Queue Worker (background thread, started at app startup)
  |     -> consumes one queued file at a time
  |     -> calls upload_service.process_task() for the actual work
  |
  |-- upload_service (orchestration)
  |     -> file_service   (filesystem: discover / move files)
  |     -> cbos_client    (network: upload to CBOS)
  |     -> repository     (database: track status)
  |
  |-- PostgreSQL (uploaded_files table)
```

### Project layout

```
app/
├── main.py                    FastAPI app + lifespan (DB, worker, scheduler startup)
├── api/v1/
│   ├── router.py               aggregates all v1 routes
│   └── endpoints/
│       ├── upload.py           POST /upload (manual upload edge case)
│       └── system.py           GET /health, POST /run-now, GET /queue-status
├── core/
│   ├── config.py               Settings (pydantic-settings, reads .env)
│   ├── database.py             SQLAlchemy engine/session, init_db()
│   ├── logging.py               structured logging setup
│   └── queue.py                 in-memory Queue + FileTask + dedup guard
├── models/
│   └── uploaded_file.py         UploadedFile ORM model
├── schemas/
│   └── upload.py                UploadResponse (Pydantic)
├── repositories/
│   └── uploaded_file_repository.py   all DB reads/writes for uploaded_files
├── services/
│   ├── file_service.py          filesystem-only: discover, move files
│   └── upload_service.py        orchestrates discovery -> queue -> upload -> DB
├── clients/
│   └── cbos_client.py           the actual CBOS HTTP call
├── workers/
│   └── upload_worker.py         background loop consuming the queue
└── scheduler/
    └── scheduler.py             APScheduler wiring, triggers scans only

scripts/                        local dev/test tooling - never imported by app/
├── dummy_cbos.py                 local CBOS simulator (POST /upload, GET /health)
├── generate_test_data.py         generates realistic test folders/files
└── run_local_test.py             end-to-end automated test harness
```

---

## Folder structure the service watches

```
{FILE_ROOT_PATH}/{segment}/{date}/{file}
```

Example:

```
edp/
├── EQ/
│   └── 2026-07-05/
│       ├── trade_001.csv
│       ├── trade_002.csv
│       ├── upload/          <- successfully uploaded files land here
│       │   └── trade_003.csv
│       └── fail/            <- failed uploads land here
│           └── trade_004.csv
├── FO/
├── CUR/
├── MCX/
└── SLBM/
```

- `date` uses `DATE_FOLDER_FORMAT` (default `%Y-%m-%d`).
- Every scan checks **both T (today) and T-1 (yesterday)**.
- `upload/` and `fail/` are created automatically and are always excluded
  from discovery - the scheduler never re-scans its own output folders.
- On success, the file is removed from its source location and moved to
  `upload/`. On failure, it's moved to `fail/` and the row's `retry_count`
  is incremented.

---

## Setup

```powershell
cd file_uploader
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create the Postgres database referenced by `DATABASE_URL` (tables are
created automatically by `init_db()` on startup - no migrations needed):

```sql
CREATE DATABASE edp_cbos;
```

### Configuration (`.env`)

| Variable | Meaning | Example |
|---|---|---|
| `FILE_ROOT_PATH` | Root folder the scheduler scans | `C:/Users/you/mofsl/edp` |
| `DATE_FOLDER_FORMAT` | strftime format for date folders | `%Y-%m-%d` |
| `POLL_INTERVAL_MINUTES` | How often the scheduler scans | `5` |
| `CBOS_UPLOAD_URL` | CBOS's single document upload endpoint | `https://cbos/api/upload` |
| `CBOS_TIMEOUT_SECONDS` | HTTP timeout for the CBOS call | `30` |
| `DATABASE_URL` | Postgres connection string | `postgresql://user:pass@host:5432/db` |

All values are read once via `app/core/config.py`'s `Settings` (pydantic-settings),
which loads `.env` automatically and matches env var names case-insensitively.

**Note:** if your Postgres password contains special characters (`@`, `#`, etc.),
URL-encode it in `DATABASE_URL` (`@` -> `%40`).

---

## Running the app

```powershell
venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload
```

On startup you should see, in this order:

```
upload_worker INFO Queue worker started
apscheduler ... Added job "_scan_job" ...
scheduler INFO Scheduler started, running every N minute(s)
Application startup complete.
```

Nothing else is required - the scheduler fires automatically on its interval
and the worker processes whatever gets queued.

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/run-now` | Trigger an immediate discovery scan (don't wait for the interval) |
| GET | `/queue-status` | `{"queue_size": N, "unfinished_tasks": N}` - queue depth / true in-flight count |
| POST | `/upload` | Manual upload edge case (multipart `file` + `segment` form fields) |
| GET | `/docs` | Swagger UI |

`POST /upload` saves the file into the standard `{segment}/{today}/` folder
and marks it `pending` - it never talks to CBOS directly. The next scheduler
scan discovers and processes it exactly like any other file.

---

## Database

Table `uploaded_files`:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `file_name` | String | |
| `file_path` | String, unique | Current location - updated when the file is moved |
| `folder_date` | String | The date folder it was discovered under |
| `segment` | String | e.g. `EQ`, `FO`, `CUR`, `MCX`, `SLBM` |
| `status` | String | `pending` \| `uploaded` \| `failed` |
| `cbos_response` | String | Raw response/error text from CBOS |
| `cbos_upload_id` | String | UUID returned by CBOS on success |
| `retry_count` | Integer | Incremented on each failed attempt |
| `uploaded_at` | DateTime | Set on success |
| `created_at` | DateTime | Row creation time |

---

## Local testing (no real CBOS needed)

`scripts/dummy_cbos.py` simulates CBOS's upload endpoint locally, with
configurable success/failure behavior so you can exercise both the
`upload/` and `fail/` code paths.

### Automated end-to-end test

```powershell
venv\Scripts\Activate.ps1
python -m scripts.run_local_test success   # expect: everything -> upload/
python -m scripts.run_local_test fail      # expect: everything -> fail/
python -m scripts.run_local_test random    # expect: a mix, retry_count > 0 on failures
```

This single command: generates fresh test data, starts the dummy CBOS server,
starts the real app, triggers a scan, waits for the queue to fully drain,
verifies the filesystem + database state, prints a report, and tears
everything down. Exit code is `0` on PASS, `1` on FAIL.

Uses `.env.test`, which points `FILE_ROOT_PATH` at a throwaway `./edp` folder
and `CBOS_UPLOAD_URL` at `http://localhost:9000/upload` (the dummy server) -
your real `.env` / production database are never touched by this.

### Manual / interactive local testing

Three terminals, all from `file_uploader/` with the venv active:

```powershell
# 1) Generate test data + start the dummy CBOS server
python -m scripts.generate_test_data
python -m uvicorn scripts.dummy_cbos:app --port 9000

# 2) Start the real app (point .env's CBOS_UPLOAD_URL at http://127.0.0.1:9000/upload first)
python -m uvicorn app.main:app --port 8000 --reload
```

```powershell
# 3) Drive it
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/run-now
curl http://127.0.0.1:8000/queue-status
curl http://127.0.0.1:9000/stats
```

`scripts/dummy_cbos.py` behavior is controlled by env vars:

| Variable | Values | Effect |
|---|---|---|
| `CBOS_SIMULATION_MODE` | `success` \| `fail` \| `random` | Forces outcome, or randomizes it |
| `CBOS_RANDOM_SUCCESS_RATE` | `0.0`-`1.0` | Success probability when mode is `random` (default `0.7`) |
| `DUMMY_CBOS_STORAGE` | path | Where "uploaded" files are saved (default `dummy_cbos_storage/`) |

---

## Troubleshooting

- **"Timed out waiting for queue to drain" in `run_local_test.py`** - check
  the dummy CBOS / app process output printed above the error; a slow or
  unreachable `CBOS_UPLOAD_URL` will stall every file.
- **Files not being discovered** - confirm they sit directly under
  `{FILE_ROOT_PATH}/{segment}/{date}/`, not inside `upload/` or `fail/`, and
  that `{date}` matches `DATE_FOLDER_FORMAT` for today or yesterday.
- **Repeated same-day test runs show unexpectedly high row counts** - the
  `uploaded_files` table isn't truncated between runs; each physical upload
  attempt is a permanent audit row by design. `TRUNCATE TABLE uploaded_files`
  before a clean comparison run.
- **Schema changes not taking effect** - `init_db()` only calls
  `create_all()`, which does not `ALTER TABLE` existing tables. Drop the
  table (dev/test only) or write a migration for schema changes.
