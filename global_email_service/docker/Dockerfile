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

ENV HOST=0.0.0.0
ENV PORT=9200
ENV LOG_LEVEL=INFO

EXPOSE 9200

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health/live')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "global_email_service.main"]
