# global_email_service

A small, standalone module: **JSON in → color-coded HTML table → email out
via Microsoft Graph.**

Built for the EDP Billing use case (notify the MOFSL ops team when a
segment/process fails, is skipped, or completes) but it is intentionally
generic — it doesn't import anything from `src/agent/edp`, so it can be
reused for any "turn this record (or list of records) into a table and
email it" use case. **Not integrated into the existing app** — call it
directly wherever/whenever you need it.

Rendering uses **Jinja2** templates (`templates/email.html.j2` /
`templates/email.txt.j2`); sending uses the **Microsoft Graph `sendMail`**
API (OAuth2 client-credentials) via `graph_client.py` — no SMTP.

Can be used either as a **Python library** (import and call directly) or
run as a **standalone HTTP service** (see "Run as a standalone service"
below) with a `POST /send` endpoint that takes the same JSON payload.

## Install / setup

This module has its **own `.env` file, separate from the repo-root `.env`**,
so the whole folder is self-contained and portable (drop it into another
project and it still works):

- `src/global_email_service/.env.example` — template, committed to git.
- `src/global_email_service/.env` — your actual local config (gitignored,
  same rule as the repo-root `.env`). Ships with `EMAIL_DRY_RUN=true` and a
  placeholder recipient so the demo works out of the box with zero setup.
  Fill in real `EMAIL_GRAPH_*` values and flip `EMAIL_DRY_RUN=false` to
  actually send.

`config.py` loads this file via `python-dotenv` at import time.
`load_dotenv()` never overrides variables already set in the real process
environment, so a real deployment's actual env vars always win over
whatever is in this `.env` file.

```env
# Azure AD app registration with application permission Mail.Send
# (admin-consented), scoped to the sender mailbox below.
EMAIL_GRAPH_TENANT_ID=<azure-ad-tenant-id>
EMAIL_GRAPH_CLIENT_ID=<app-registration-client-id>
EMAIL_GRAPH_CLIENT_SECRET=********
EMAIL_GRAPH_SENDER=rms@covasant.com
EMAIL_GRAPH_TIMEOUT_SECONDS=15

EMAIL_FROM_NAME=EDP Billing Alerts

EMAIL_DEFAULT_TO=mofsl-ops@example.com,team-lead@example.com
EMAIL_DEFAULT_CC=

EMAIL_MAX_RETRIES=2
EMAIL_RETRY_BACKOFF_SECONDS=2

EMAIL_DRY_RUN=false              # true -> log the rendered email instead of calling Graph
```

### Getting Graph credentials

1. Register an app in Azure AD (Entra ID) → API permissions → add
   **Mail.Send** (Application, not Delegated) → grant admin consent.
2. Create a client secret under Certificates & secrets.
3. The sender mailbox (`EMAIL_GRAPH_SENDER`, default `rms@covasant.com`)
   must be a real mailbox in that same tenant — Graph sends *as* that
   mailbox, it does not need its own password.

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
bad input / Graph send failure.

## Run as a standalone service

`main.py` exposes this module as its own FastAPI app — same pattern as
`mock_cbos` — so other systems can call it over HTTP without importing
Python code directly:

```powershell
python -m src.global_email_service.main
# or explicitly:
uvicorn src.global_email_service.main:app --host 0.0.0.0 --port 9200 --reload
```

| Endpoint | Method | Graph needed? | Purpose |
|---|---|---|---|
| `/health` | GET | no | Shows Graph sender/config status + dry-run mode |
| `/health/ready` | GET | no | CAMS-style readiness probe |
| `/health/live` | GET | no | CAMS-style liveness probe |
| `/send` | POST | yes | Full flow: validate → render → email |
| `/preview` | POST | no | Same payload, returns the rendered HTML body directly (no send) |
| `/preview.text` | POST | no | Same, plain-text fallback body |

```bash
curl -X POST http://localhost:9200/send \
  -H "Content-Type: application/json" \
  -d @src/global_email_service/examples/sample_mcx_recon_failure.json
```

Host/port/log level are read from `HOST` / `PORT` / `LOG_LEVEL` (CAMS
convention), falling back to this module's own `EMAIL_SERVICE_HOST` /
`EMAIL_SERVICE_PORT` for backward compatibility — defaults are `0.0.0.0` /
`9200` / `INFO`. Swagger UI is available at `/docs` for manual testing.

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
# Render only (no Graph call):
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
| `config.py` | `EmailServiceConfig` + `load_email_config()` — Graph/env settings; `load_server_settings()` for host/port/log level |
| `colors.py` | Status/severity → row color resolution |
| `table_renderer.py` | Derives columns/cell text/colors/severity from rows; hands them to the Jinja templates |
| `templating.py` | Jinja2 `Environment` + template loading |
| `templates/email.html.j2` | HTML email body template (autoescaped) |
| `templates/email.txt.j2` | Plain-text email body template (CLI/preview use; Graph itself is only sent HTML) |
| `graph_client.py` | Microsoft Graph `sendMail` — OAuth2 client-credentials token + retry on transient errors |
| `service.py` | `send_alert_email()` / `send_segment_alert()` — payload validation + orchestration |
| `main.py` | Standalone FastAPI app (`/send`, `/preview`, `/health*`) — run this to expose the service over HTTP |
| `exceptions.py` | `InvalidPayloadError`, `EmailSendError` |
| `demo.py` | Standalone CLI demo (see above) |
| `examples/*.json` | Sample payloads: cash success, SLB GTG failure, MCX recon EOD summary |
| `requirements.txt` | This module's own (public-only) dependencies, for standalone build/deploy |
| `docker/Dockerfile`, `docker/docker-entrypoint.sh` | CAMS-style container build for deploying this module as its own service |

## Reviewed: `mofsl_common_lib-main` (what's reused, what isn't)

`mofsl_common_lib-main/` in this folder is MOFSL's shared automation
framework (workflow engine, executors, scheduler, DB state store, etc.) —
much bigger in scope than this module needs. What was actually useful and
adopted:

- **`infra/graph.py`'s `GraphClient`** — the OAuth2 client-credentials +
  `sendMail` flow is exactly what `graph_client.py` here implements
  (adapted to be synchronous and dependency-free, since this module
  intentionally avoids taking a hard dependency on the whole
  `mofsl-automation` package for a single HTTP call).
- **`infra/errors.py`'s transient/permanent classification** — the same
  idea (401/403/400/404/422 = fail fast, everything else = retry) is
  applied to Graph's HTTP responses in `graph_client.send_message()`.

Not adopted (out of scope for a single email-sending module): the
workflow/step engine, `StateStore`/DB persistence, `SecretProvider`
(AWS Secrets Manager) — this module reads credentials straight from env
vars/`.env`, matching how the rest of this repo's config works. If this
service is later deployed with a real secrets manager, swap
`load_email_config()`'s `os.getenv()` calls for that provider without
touching `graph_client.py` or `service.py`.

## Deploying into CAMS

This module is a self-contained FastAPI service, so it deploys the same
way as the main agent (see repo-root `docker/Dockerfile`), just scoped to
its own folder:

```powershell
docker build -f src/global_email_service/docker/Dockerfile -t global-email-service .
docker run -p 9200:9200 --env-file src/global_email_service/.env global-email-service
```

- `/health`, `/health/ready`, `/health/live` follow the same probe
  convention as `src/agent/__main__.py`. `/health` and `/health/ready`
  return **503** if the service is not in `EMAIL_DRY_RUN=true` mode and any
  of the three required Graph credentials are missing — so CAMS won't
  route traffic to (or will restart) an instance that can't actually send
  mail.
- `HOST` / `PORT` / `LOG_LEVEL` env vars match CAMS naming (see
  `.env.example`).
- `requirements.txt` is self-contained by design: build pulls only public
  PyPI packages (`fastapi`, `uvicorn`, `jinja2`, `python-dotenv`, `httpx`),
  so no GCP Artifact Registry credentials are needed for this module's
  image.
- Supply real `EMAIL_GRAPH_*` secrets via the CAMS secrets/config
  injection mechanism — either as plain env vars, or as mounted secret
  files, in which case point `EMAIL_GRAPH_TENANT_ID_FILE` /
  `EMAIL_GRAPH_CLIENT_ID_FILE` / `EMAIL_GRAPH_CLIENT_SECRET_FILE` at the
  mounted path instead (each `_FILE` variant is read and stripped at
  startup). Never bake secrets into the image.
- `uvicorn --reload` is **off** by default and must stay off in the
  container (set only via `UVICORN_RELOAD=true` for local dev outside
  Docker) — it's a dev-only file-watcher feature and unsafe/wasteful in a
  production container.
- If `cams_otel_lib` is present in the runtime environment (CAMS injects
  it), this service auto-detects it at startup and initializes OTEL
  tracing/logging via `Otel_Client.initialize_otel_client(...)`; outside
  CAMS it silently falls back to plain stdlib logging. No code changes or
  extra requirements needed either way.

## Future integration (not done yet, by design)

When you're ready to wire this into the EDP agent (e.g. calling
`send_segment_alert(serialize_segment(row))` from `pipeline/stages.py::_fail()`),
the row shape from `utils/serializers.py::serialize_segment()` already maps
directly onto what this module expects — no adapter needed.
