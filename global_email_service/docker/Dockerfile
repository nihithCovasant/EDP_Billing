# syntax=docker/dockerfile:1
# CAMS-style multi-stage build for global_email_service as its own deployable
# unit. Build context = this project's own root (the `global_email_service`
# folder), so this module stays fully self-contained and portable:
#   cd global_email_service && docker build -f docker/Dockerfile -t global-email-service .
#
# Installs via pyproject.toml (pip install ".[api]") rather than a separate
# requirements.txt — the package + its "api" extra (fastapi/uvicorn, needed
# only to run main.py as an HTTP service) are the single source of truth for
# dependencies, same as any other pip-installable library.
FROM python:3.12-slim AS builder

WORKDIR /build

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --upgrade pip && pip install --no-cache-dir ".[api]"

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh

# Config source: at startup config.py's _apply_agent_config_env() bridges the
# host project's agent_config.json `env` block into the environment (single
# source of truth). This standalone image does NOT bundle agent_config.json, so
# to run it config-driven, mount that file and set APP_CONFIG_PATH, e.g.:
#   -v /path/to/agent_config.json:/app/config/agent_config.json:ro \
#   -e APP_CONFIG_PATH=/app/config/agent_config.json
# The ENV vars below are the fallback when no config file is provided. Note the
# server binds EMAIL_SERVICE_PORT first, then PORT (see load_server_settings()),
# so the email service never collides with the main agent's PORT (8005).
ENV EMAIL_SERVICE_HOST=0.0.0.0
ENV EMAIL_SERVICE_PORT=9200
ENV LOG_LEVEL=INFO

EXPOSE 9200

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9200/health/live')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "global_email_service.main"]
