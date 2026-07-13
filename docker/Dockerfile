# syntax=docker/dockerfile:1
# Multi-stage build for optimized production image
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
# requirements.txt has "-e ./global_email_service" (local sibling package,
# used for terminal-status email alerts — see
# src/agent/edp/repository/segment.py::move_to_state()) — must be present
# in the build context BEFORE pip install, or that line fails to resolve.
COPY global_email_service/ ./global_email_service/

RUN pip install --upgrade pip \
    && pip install keyring keyrings.google-artifactregistry-auth google-auth

# Pull requirements from GCP Artifact Registry (private packages like platform-sdk-common)
# and public PyPI. The GCP service account key is mounted as a build secret.
RUN --mount=type=secret,id=gcp_reader_sa \
    export GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp_reader_sa && \
    ACCESS_TOKEN=$(python3 -c "import google.auth; import google.auth.transport.requests; creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform']); creds.refresh(google.auth.transport.requests.Request()); print(creds.token)") && \
    pip install -r requirements.txt \
        --index-url https://oauth2accesstoken:${ACCESS_TOKEN}@us-central1-python.pkg.dev/cams-151225/cams-python-repo/simple/ \
        --extra-index-url https://pypi.org/simple/ \
        --no-input && \
    pip uninstall -y keyring keyrings.google-artifactregistry-auth

# Production stage
FROM python:3.12-slim

WORKDIR /app

# Copy venv from builder
COPY --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# Copy application code
COPY src/ ./src/

# global_email_service is an editable ("-e") install — its .pth file points
# back at this source tree, so it must exist at runtime too, not just at
# build time. alembic/ + alembic.ini are needed because init_database()
# runs "alembic upgrade head" automatically on every agent startup (see
# src/agent/edp/migrations.py / database.py::init_database()) — no separate
# migration step is required in this entrypoint.
COPY global_email_service/ ./global_email_service/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Copy entrypoint script and fix line endings (Windows CRLF -> Unix LF)
COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh

# Create directories for configs and secrets
RUN mkdir -p /app/config /app/secrets

# NOTE: Runtime configuration (HOST, PORT, LOG_LEVEL, AGENT_NAME, OTEL_*, EDP_*,
# EMAIL_*, ...) is NOT set here as env vars. It lives in agent_config.json's
# `agent_config.env` block and is bridged into the environment at startup by
# apply_config_env() (src/config/settings.py) — agent_config.json is the
# single source of truth. Because that bridge uses os.environ.setdefault(), any
# env var set here would OVERRIDE the config, so we deliberately set none.
# Override per-deployment only by passing an explicit env var or mounting an
# alternate config via APP_CONFIG_PATH.
#
# The port literal below (8005) is a build-time contract for EXPOSE/HEALTHCHECK
# and MUST match agent_config.json -> agent_config.env.PORT.
EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8005/health/live')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "src.agent"]
