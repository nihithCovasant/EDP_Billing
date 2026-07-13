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
docker compose up --build
```

No `.env` file and no environment variables are required. `docker-compose.yml`
builds `agent` from `docker/Dockerfile` and mounts `agent_config.json` read-only
— **all** configuration (server, LLM via the LiteLLM gateway, CBOS, EDP wake
loop, email alerts, and the database connection string) is read from that one
file. The stack starts **no** local postgres container: the agent connects to
whatever `agent_config.json → secrets.database.postgres.connection_string`
points at, so that database host must be reachable from the container.

To change any setting, edit `src/config/agent_config.json` (its
`agent_config.env` / `.secrets` / `.edp` blocks) — see the repo-level config
docs. To override a single value for one run, pass an explicit `-e VAR=...`
(explicit env vars still win over the config file).

### Standalone `docker run`

```bash
docker run -d \
  -p 8005:8005 \
  -v "$(pwd)/src/config/agent_config.json:/app/config/agent_config.json:ro" \
  edp-billing-agent:latest
```

That's it — every setting comes from the mounted `agent_config.json`. The
entrypoint copies it to the internal path at startup; alternatively supply it
via `APP_CONFIG_PATH` (path to a mounted file) or `CONFIG_JSON` (whole file as a
JSON string) — see `docker/docker-entrypoint.sh`'s `inject_config()` for the
priority order.

## Configuration

`agent_config.json` is the **single source of truth**. At startup
`apply_config_env()` (`src/config/settings.py`) bridges its
`agent_config.env` block into the process environment, so every existing
env-reading code path (pydantic `Settings`, `src/agent/edp/config.py`,
`cams_otel_lib`, `global_email_service`, ...) is fed from the file — no `.env`
and no `-e` flags required.

Edit these blocks in `src/config/agent_config.json`:

| Block | Owns |
|---|---|
| `agent_config.env` | Server (`HOST`/`PORT`/`LOG_LEVEL`), `AGENT_NAME`, OTEL flags, `EDP_WAKE_INTERVAL_SECONDS`, `EDP_LOOP_ENABLED`, all `EMAIL_*` (dry-run, Graph credentials, recipients), `CBOS_*` overrides |
| `agent_config.secrets` | LiteLLM gateway (`base_url`/`api_key`), `database.postgres.connection_string`, Pinecone, `edpb_download` |
| `agent_config.edp` | CBOS URLs / mock flag / login IDs, and the 9 segment / 5 post-trade-process window definitions |

Because the bridge uses `os.environ.setdefault()`, an explicit env var
(e.g. `-e PORT=9000`) still **overrides** the file for that run — use that for
one-off per-deployment tweaks, secret injection from a secret manager, etc.

The database is whatever `secrets.database.postgres.connection_string` resolves
to — Alembic migrations run automatically at startup (async
`postgresql+asyncpg://` is auto-converted to sync `postgresql+psycopg://`).

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
