# global_email_service

A small, standalone module: **JSON in → color-coded HTML table → email out
over SMTP.**

Built for the EDP Billing use case (notify the MOFSL ops team when a
segment/process fails, is skipped, or completes) but it is intentionally
generic — it doesn't import anything from `src/agent/edp`, so it can be
reused for any "turn this record (or list of records) into a table and
email it" use case. **Not integrated into the existing app** — call it
directly wherever/whenever you need it.

Rendering uses **Jinja2** templates (`templates/email.html.j2` /
`templates/email.txt.j2`); sending uses only `smtplib`/`email` from the
standard library.

Can be used either as a **Python library** (import and call directly) or
run as a **standalone HTTP service** (see "Run as a standalone service"
below) with a `POST /send` endpoint that takes the same JSON payload.

## Install / setup

Nothing to install. This module has its **own `.env` file, separate from
the repo-root `.env`**, so the whole folder is self-contained and portable
(drop it into another project and it still works):

- `src/global_email_service/.env.example` — template, committed to git.
- `src/global_email_service/.env` — your actual local config (gitignored,
  same rule as the repo-root `.env`). Ships with `EMAIL_DRY_RUN=true` and a
  placeholder recipient so the demo works out of the box with zero setup.
  Fill in real `EMAIL_SMTP_*` values and flip `EMAIL_DRY_RUN=false` to
  actually send.

`config.py` loads this file via `python-dotenv` at import time.
`load_dotenv()` never overrides variables already set in the real process
environment, so a real deployment's actual env vars always win over
whatever is in this `.env` file.

```env
EMAIL_SMTP_HOST=smtp.yourcompany.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USERNAME=alerts@yourcompany.com
EMAIL_SMTP_PASSWORD=********
EMAIL_SMTP_USE_TLS=true          # STARTTLS (typical for port 587)
EMAIL_SMTP_USE_SSL=false         # implicit TLS (typical for port 465)
EMAIL_SMTP_TIMEOUT_SECONDS=15

EMAIL_FROM_ADDRESS=alerts@yourcompany.com
EMAIL_FROM_NAME=EDP Billing Alerts

EMAIL_DEFAULT_TO=mofsl-ops@example.com,team-lead@example.com
EMAIL_DEFAULT_CC=

EMAIL_MAX_RETRIES=2
EMAIL_RETRY_BACKOFF_SECONDS=2

EMAIL_DRY_RUN=false              # true -> log the rendered email instead of sending
```

## Quick usage

```python
from src.global_email_service import send_alert_email, send_segment_alert

# Option A — a single "row" (matches segment_execution shape)
send_segment_alert({
    "trade_date": "2026-07-04",
    "segment_code": "EQ",
    "segment_name": "Cash",
    "segment_status": "FAILED",
    "current_process": "BILLPOSTING",
    "current_phase": "AWAIT_BILLPOSTING",
    "skip_category": "CBOS_ERROR",
    "skip_reason": "BILLPOSTING check error: ...",
    "started_at": "2026-07-04T09:15:00+05:30",
    "completed_at": "2026-07-04T09:22:41+05:30",
})

# Option B — full control via send_alert_email(payload)
send_alert_email({
    "subject": "EDP Alert: EQ segment FAILED",
    "to": ["mofsl-ops@example.com"],
    "row": { ... same row as above ... },
})

# Option C — multiple rows in one table (e.g. a full day's summary)
send_alert_email({
    "subject": "EDP Daily Status — 2026-07-04",
    "to": ["mofsl-ops@example.com"],
    "rows": [ {...row1...}, {...row2...}, ... ],
})
```

Both calls return an `EmailSendResult(success, message, subject, to, cc, dry_run)`
and raise `InvalidPayloadError` / `EmailSendError` (see `exceptions.py`) on
bad input / SMTP failure.

## Run as a standalone service

`main.py` exposes this module as its own FastAPI app — same pattern as
`mock_cbos` — so other systems can call it over HTTP without importing
Python code directly:

```powershell
python -m src.global_email_service.main
# or explicitly:
uvicorn src.global_email_service.main:app --host 0.0.0.0 --port 9200 --reload
```

| Endpoint | Method | SMTP needed? | Purpose |
|---|---|---|---|
| `/health` | GET | no | Liveness + shows which SMTP host/dry-run mode is active |
| `/send` | POST | yes | Full flow: validate → render → email |
| `/preview` | POST | no | Same payload, returns the rendered HTML body directly (no send) |
| `/preview.text` | POST | no | Same, plain-text fallback body |

```bash
curl -X POST http://localhost:9200/send \
  -H "Content-Type: application/json" \
  -d @src/global_email_service/examples/sample_mcx_recon_failure.json
```

Port/host are overridable via `EMAIL_SERVICE_PORT` / `EMAIL_SERVICE_HOST`
env vars (defaults `9200` / `0.0.0.0`). Swagger UI is available at
`/docs` for manual testing.

## Payload contract

| Key | Type | Required | Meaning |
|---|---|---|---|
| `rows` | `list[dict]` | one of `rows`/`row`/flat fields | Multiple records → multi-row table |
| `row` | `dict` | " | Single record → single-row table |
| *(flat fields)* | — | " | If neither `rows` nor `row` is given, every top-level key that isn't one of the metadata keys below is treated as one row's fields |
| `to` | `list[str]` \| `str` (csv) | no | Recipients. Falls back to `EMAIL_DEFAULT_TO` if omitted |
| `cc` / `bcc` | `list[str]` \| `str` | no | Same as `to` |
| `subject` | `str` | no | Auto-generated from the row(s) if omitted (e.g. `"EDP Alert: EQ — FAILED"`) |
| `title` | `str` | no | Heading shown above the table in the email body |
| `summary` | `str` | no | Short paragraph shown above the table |
| `columns` | `list[str]` | no | Explicit column order/subset. Default: see below |
| `color_overrides` | `dict[str, [bg, fg]]` | no | Extend/override the default status→color map (see `colors.py`) |

Row data itself is completely free-form — any JSON-serializable dict.
Nested dicts/lists (e.g. a `processes_json` blob) are flattened into
readable `key: value` text inside the cell rather than raw JSON.

### Default columns — always includes timing/process context

If you don't pass explicit `columns`, and a row has a `segment_code` key
(i.e. it looks like a segment_execution-style record), the table uses a
canonical column order instead of "whatever keys happened to be in the
payload":

```
trade_date, segment_code, segment_name, segment_status,
current_process, current_phase, process_id, skip_reason,
started_at, completed_at
```

Any field missing from a given row is shown as `—` rather than the
column disappearing — so a customer-facing email always shows *when*
something happened, even if the caller only supplied a couple of fields.
Any extra keys not in this list are appended after it, still shown.
`sequence_order` and `skip_category` are always omitted from the table
(internal pipeline detail).

Customer-facing display labels are applied at render time (payload keys stay
unchanged): `COMPLETED` → **Succeeded**, `FAILED` → **Failed**,
`skip_reason` → **Remarks**,
`current_phase` → **Stage** (Good to Go / Triggering / Completion),
and process names are expanded (e.g. `RECON` → Reconciliation).

Non-segment payloads (no `segment_code`) fall back to the union of every
row's keys, first-seen order, exactly as before.

The same `rows` shape is used whether you're sending a single-segment
failure alert (`rows` with 1 entry, or `row`) or a full end-of-day
consolidated report (`rows` with every segment for the day) — there is
no separate payload format for the two use cases.

### Severity banner

The email body always opens with a colored banner summarizing the worst
status across all rows (red "ACTION REQUIRED" if anything `FAILED`,
amber "REVIEW REQUIRED" if anything was `SKIPPED`/timed out, blue "IN
PROGRESS" if anything is still pending, green "ALL CLEAR" otherwise) —
so a reader can tell what to do at a glance before reading the table.

## Row coloring ("kind of alert")

Each row's background color is resolved (`colors.py::resolve_row_style`) by:

1. An explicit `"color"` key on the row, if present — always wins.
2. Otherwise, the first of `severity` / `alert_level` / `segment_status` /
   `status` / `state` present on the row is matched (case-insensitively)
   against a default map:

   | Status values | Color |
   |---|---|
   | `FAILED`, `ERROR`, `CRITICAL`, `CBOS_ERROR` | 🔴 red |
   | `SKIPPED`, `TIMEOUT`, `WARNING`, `AGENT_RESTART`, `MANUAL_SKIP` | 🟡 yellow |
   | `IN_PROGRESS`, `RUNNING`, `PENDING`, `BLOCKED`, `INFO` | 🔵 blue |
   | `COMPLETED`, `SUCCESS`, `OK`, `DONE` | 🟢 green |
   | anything else / missing | ⚪ grey |

   Pass `color_overrides` in the payload to add/override entries in this
   map without editing code.

## Try it without any setup

```powershell
# Render only (no SMTP):
python -m src.global_email_service.demo --render-only

# Cash segment success example:
python -m src.global_email_service.demo

# MCX recon failure (multi-row EOD summary):
python -m src.global_email_service.demo --file src/global_email_service/examples/sample_mcx_recon_failure.json

# SLB Good-to-Go failure:
python -m src.global_email_service.demo --file src/global_email_service/examples/sample_slbm_gtg_failed.json
```

## Files

| File | Purpose |
|---|---|
| `config.py` | `EmailServiceConfig` + `load_email_config()` — SMTP settings from env vars |
| `colors.py` | Status/severity → row color resolution |
| `table_renderer.py` | Derives columns/cell text/colors/severity from rows; hands them to the Jinja templates |
| `templating.py` | Jinja2 `Environment` + template loading |
| `templates/email.html.j2` | HTML email body template (autoescaped) |
| `templates/email.txt.j2` | Plain-text email body template |
| `smtp_client.py` | Builds the MIME message and sends it via `smtplib`, with retry on transient errors |
| `service.py` | `send_alert_email()` / `send_segment_alert()` — payload validation + orchestration |
| `main.py` | Standalone FastAPI app (`/send`, `/preview`, `/health`) — run this to expose the service over HTTP |
| `exceptions.py` | `InvalidPayloadError`, `EmailSendError` |
| `demo.py` | Standalone CLI demo (see above) |
| `examples/*.json` | Sample payloads: cash success, SLB GTG failure, MCX recon EOD summary |

## Future integration (not done yet, by design)

When you're ready to wire this into the EDP agent (e.g. calling
`send_segment_alert(serialize_segment(row))` from `pipeline/stages.py::_fail()`),
the row shape from `utils/serializers.py::serialize_segment()` already maps
directly onto what this module expects — no adapter needed.
