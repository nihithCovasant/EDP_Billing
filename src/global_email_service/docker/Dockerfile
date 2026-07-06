# syntax=docker/dockerfile:1
# CAMS-style multi-stage build for global_email_service as its own deployable
# unit — same pattern as the main agent's docker/Dockerfile, but scoped to
# this module's own (public-only) dependencies, so no GCP Artifact Registry
# credentials are needed at build time.
FROM python:3.12-slim AS builder

WORKDIR /app

COPY src/global_email_service/requirements.txt .
RUN python3 -m venv venv
ENV PATH="/app/venv/bin:$PATH"
RUN pip install --upgrade pip && pip install -r requirements.txt

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY src/global_email_service/ ./src/global_email_service/
COPY src/global_email_service/docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh

ENV HOST=0.0.0.0
ENV PORT=9200
ENV LOG_LEVEL=INFO

EXPOSE 9200

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health/live')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "src.global_email_service.main"]
