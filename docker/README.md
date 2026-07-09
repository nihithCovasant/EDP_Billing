# Docker build & deployment

## What's in the image

`docker/Dockerfile` is a two-stage build:

1. **builder** — creates a venv and installs `requirements.txt`, including the
   local editable sibling package `global_email_service` (terminal-status
   email alerts — see `src/agent/edp/repository/segment.py::move_to_state()`).
2. **final** — copies the venv plus everything needed at runtime:
   - `src/` — the agent code
   - `global_email_service/` — required because it's installed with `pip install -e`,
     so its `.pth` file points back at this source tree, not a copied wheel
   - `alembic/`, `alembic.ini` — `init_database()` runs `alembic upgrade head`
     automatically on every agent startup (`src/agent/edp/migrations.py`), so
     there is **no separate migration step/command** in this image or its
     entrypoint
   - `docker/docker-entrypoint.sh` — injects `agent_config.json` from an env var
     or mounted volume, then execs the CMD

Build context must be the **repo root** (not `docker/`), since the Dockerfile
copies `global_email_service/`, `src/`, `alembic/` from there:

```bash
docker build -f docker/Dockerfile -t edp-billing-agent:latest .
```

The builder stage pulls private packages (e.g. `platform-sdk-common`) from a
GCP Artifact Registry using a mounted build secret — a service-account JSON
key with Artifact Registry Reader access:

```bash
docker build -f docker/Dockerfile \
  --secret id=gcp_reader_sa,src=/path/to/gcp-reader-sa.json \
  -t edp-billing-agent:latest .
```

## Running it

### Quick local stack (docker-compose)

```bash
cp .env.example .env   # fill in DB_PASSWORD, CBOS_*, EMAIL_* as needed
docker compose up --build
```

This starts `postgres` (with a persisted volume) and `agent` (built from
`docker/Dockerfile`), wired together via `docker-compose.yml`. Required env
vars (`DB_PASSWORD`, `CBOS_STATUS_URL`, `CBOS_PROCESS_URL`) will fail the
compose run with a clear error if left unset — see that file for the full
list and defaults.

### Standalone `docker run`

```bash
docker run -d \
  -p 8005:8005 \
  -v "$(pwd)/src/config/agent_config.json:/app/config/agent_config.json:ro" \
  -e DATABASE_URL="postgresql+asyncpg://user:pass@db-host:5432/EDP_Billing" \
  -e CBOS_STATUS_URL="https://cbos-host:8087" \
  -e CBOS_PROCESS_URL="https://cbos-host:8003" \
  -e CBOS_USE_MOCK=false \
  -e EMAIL_DRY_RUN=false \
  -e EMAIL_GRAPH_TENANT_ID="..." \
  -e EMAIL_GRAPH_CLIENT_ID="..." \
  -e EMAIL_GRAPH_CLIENT_SECRET="..." \
  -e EMAIL_DEFAULT_TO="mofsl-ops@example.com" \
  edp-billing-agent:latest
```

`agent_config.json` can instead be supplied via the `CONFIG_JSON` env var
(whole file as a JSON string) or `APP_CONFIG_PATH` — see
`docker/docker-entrypoint.sh`'s `inject_config()` for the priority order.

## Environment variables

All EDP-specific settings resolve with **env vars taking priority over
`agent_config.json`**, then hardcoded defaults (see
`src/agent/edp/config.py::load_edp_config()`). Full reference: `.env.example`.

| Variable | Purpose |
|---|---|
| `EDP_WAKE_INTERVAL_SECONDS` | Seconds between wake cycles (default 60) |
| `CBOS_STATUS_URL`, `CBOS_PROCESS_URL` | CBOS base URLs (real system or `mock_cbos`) |
| `CBOS_USE_MOCK` | `true` = in-process mock responses, `false` = real HTTP calls to the URLs above |
| `CBOS_LOGIN_ID` | LOGINID used for the 10 real-segment CBOS calls |
| `POST_TRADE_LOGIN_ID` | LOGINID used for the 5 post-trade CBOS calls |
| `DATABASE_URL` **or** `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USERNAME`/`DB_PASSWORD` | PostgreSQL connection (async `postgresql+asyncpg://`; Alembic migrations auto-convert to sync `postgresql+psycopg://`) |
| `EMAIL_DRY_RUN` | `true` = log rendered email only, `false` = send via Microsoft Graph |
| `EMAIL_GRAPH_TENANT_ID`, `EMAIL_GRAPH_CLIENT_ID`, `EMAIL_GRAPH_CLIENT_SECRET`, `EMAIL_GRAPH_SENDER` | Microsoft Graph app-registration credentials for `global_email_service` |
| `EMAIL_DEFAULT_TO`, `EMAIL_DEFAULT_CC` | Recipients for terminal-status alerts (comma-separated) |

`agent_config.json` still owns the 10 segment / 5 post-trade-process window
definitions (mounted read-only into the container — see above).

## Database migrations

No manual step needed — `init_database()` runs `alembic upgrade head`
automatically (off the event loop, via `asyncio.to_thread`) every time the
agent starts, whether the schema is brand new or already at head. This only
works because `alembic/` and `alembic.ini` are copied into the final image
stage; if you ever see `RuntimeError: alembic.ini not found`, the image was
built without them.

## Health check

The image's `HEALTHCHECK` and `docker-compose.yml`'s healthcheck both hit
`GET /health/live` on port 8005.
