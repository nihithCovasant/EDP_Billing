"""Standalone FastAPI app for the global email service."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse

from .config import load_email_config
from .exceptions import EmailSendError, InvalidPayloadError
from .service import parse_payload, send_alert_email
from .table_renderer import render_email_body

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("global_email_service")

app = FastAPI(
    title="Global Email Service",
    description="JSON in -> color-coded HTML table -> SMTP email out.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> Dict[str, Any]:
    config = load_email_config()
    return {
        "status": "ok",
        "service": "global-email-service",
        "dry_run": config.dry_run,
        "smtp_host": config.smtp_host,
        "smtp_port": config.smtp_port,
        "default_to_configured": bool(config.default_to),
    }


@app.post("/send")
async def send(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = send_alert_email(payload)
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EmailSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info(
        "POST /send: success=%s dry_run=%s subject=%r to=%s",
        result.success, result.dry_run, result.subject, result.to,
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
async def preview(payload: Dict[str, Any]) -> str:
    try:
        request = parse_payload(payload)
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    html_body, _ = render_email_body(
        request.rows, title=request.title, summary=request.summary,
        columns=request.columns, color_overrides=request.color_overrides,
    )
    return html_body


@app.post("/preview.text", response_class=PlainTextResponse)
async def preview_text(payload: Dict[str, Any]) -> str:
    try:
        request = parse_payload(payload)
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _, text_body = render_email_body(
        request.rows, title=request.title, summary=request.summary,
        columns=request.columns, color_overrides=request.color_overrides,
    )
    return text_body


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("EMAIL_SERVICE_PORT", "9200"))
    host = os.getenv("EMAIL_SERVICE_HOST", "0.0.0.0")
    uvicorn.run("src.global_email_service.main:app", host=host, port=port, reload=True)
