"""Standalone FastAPI app for the global email service — deployable as its
own CAMS service (see README "Deploying into CAMS")."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse

from .config import load_email_config, load_server_settings
from .exceptions import EmailSendError, InvalidPayloadError
from .service import parse_payload, send_alert_email
from .table_renderer import render_email_body

# cams_otel_lib is a CAMS-platform-private package — optional so this module
# still runs standalone (outside CAMS) with plain stdlib logging.
try:
    from cams_otel_lib import Logger as _CamsLogger  # type: ignore
    from cams_otel_lib import Otel_Client

    _CamsLogger.info("global_email_service: cams_otel_lib detected — OTEL enabled")
    Otel_Client.initialize_otel_client(
        service_name="global-email-service",
        environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
    )
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("global_email_service")

app = FastAPI(
    title="Global Email Service",
    description="JSON in -> color-coded HTML table -> email out via Microsoft Graph.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _is_misconfigured(config) -> bool:
    """True when this instance cannot actually send mail: not in dry-run mode
    but missing one or more required Microsoft Graph credentials."""
    return not config.dry_run and not (config.graph_tenant_id and config.graph_client_id and config.graph_client_secret)


@app.get("/health")
async def health() -> Response:
    config = load_email_config()
    misconfigured = _is_misconfigured(config)
    body = {
        "status": "unhealthy" if misconfigured else "ok",
        "service": "global-email-service",
        "dry_run": config.dry_run,
        "graph_sender": config.graph_sender,
        "graph_configured": bool(config.graph_tenant_id and config.graph_client_id and config.graph_client_secret),
        "default_to_configured": bool(config.default_to),
    }
    return Response(
        content=json.dumps(body),
        status_code=503 if misconfigured else 200,
        media_type="application/json",
    )


@app.get("/health/ready")
async def readiness() -> Response:
    """CAMS-style readiness probe. Not ready when Graph isn't configured and
    dry-run is off — `/send` would fail on every request in that state."""
    config = load_email_config()
    ready = not _is_misconfigured(config)
    return Response(
        content=json.dumps({"status": "ready" if ready else "not ready"}),
        status_code=200 if ready else 503,
        media_type="application/json",
    )


@app.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@app.post("/send")
async def send(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        result = send_alert_email(payload)
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EmailSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info(
        "POST /send: success=%s dry_run=%s subject=%r to=%s",
        result.success,
        result.dry_run,
        result.subject,
        result.to,
    )
    return {
        "success": result.success,
        "message": result.message,
        "subject": result.subject,
        "to": result.to,
        "cc": result.cc,
        "dry_run": result.dry_run,
    }


@app.post("/preview", response_class=HTMLResponse)
async def preview(payload: dict[str, Any]) -> str:
    try:
        request = parse_payload(payload)
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    html_body, _ = render_email_body(
        request.rows,
        title=request.title,
        summary=request.summary,
        columns=request.columns,
        color_overrides=request.color_overrides,
    )
    return html_body


@app.post("/preview.text", response_class=PlainTextResponse)
async def preview_text(payload: dict[str, Any]) -> str:
    try:
        request = parse_payload(payload)
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _, text_body = render_email_body(
        request.rows,
        title=request.title,
        summary=request.summary,
        columns=request.columns,
        color_overrides=request.color_overrides,
    )
    return text_body


if __name__ == "__main__":
    import uvicorn

    host, port, log_level = load_server_settings()
    # Auto-reload is a local-dev convenience only — never enable it in a
    # container/production run (unstable process model, wastes a file
    # watcher, and silently ignored `--reload` code edits inside a slim
    # image are a common source of "why isn't my change live" confusion).
    reload_enabled = os.getenv("UVICORN_RELOAD", "false").strip().lower() in ("1", "true", "yes")
    uvicorn.run(
        "global_email_service.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        log_level=log_level.lower(),
    )
