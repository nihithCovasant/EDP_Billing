"""
EDPB file download tool for the chat agent.

Lets a user ask in chat (e.g. "download file with filename VN_09072026.txt")
and the agent calls the EDPB download API with the fixed portal/credentials
plus that filename. The real API contract (URL + exact response shape) is
still TBD with the EDPB team, so this deliberately just wires up the
request/response plumbing for now — refine parsing once the real API is
confirmed. See EDPB_DOWNLOAD_API_URL / EDPB_* env vars below to point this
at the real endpoint and credentials without touching this file.

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed. Completely independent of src/agent/edp/** (the EDP
billing wake loop/state machine); this file is never imported from there.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Optional

import httpx
from langchain_core.tools import tool
from cams_otel_lib import Logger as logger

# Placeholder — the real EDPB download endpoint URL isn't finalized yet.
_DEFAULT_API_URL = "http://localhost:9300/api/edpb/download"


def _today() -> str:
    return date.today().isoformat()


@tool
async def download_file(filename: str, trade_date: Optional[str] = None) -> str:
    """
    Download a file from the EDPB portal by filename.

    Use this when the user asks to "download file with filename <name>" (or
    similar phrasing). `filename` is required — the exact file name the user
    mentioned. `trade_date` is optional, format YYYY-MM-DD — if the user
    doesn't mention one, today's date is used automatically.
    """
    api_url = os.getenv("EDPB_DOWNLOAD_API_URL", _DEFAULT_API_URL)
    resolved_trade_date = trade_date or _today()

    payload = {
        "portal": os.getenv("EDPB_PORTAL", "bse_edpb"),
        "member_code": os.getenv("EDPB_MEMBER_CODE", "0446"),
        "user_id": os.getenv("EDPB_USER_ID", "0446"),
        "password": os.getenv("EDPB_PASSWORD", "Mosl@5555"),
        "trade_date": resolved_trade_date,
        "edpb_action": os.getenv("EDPB_ACTION", "vn"),
        "filename": filename,
    }

    logger.info(
        f"[EDPB_DOWNLOAD] filename={filename} trade_date={resolved_trade_date} "
        f"| POST {api_url}"
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(api_url, json=payload)
    except Exception as exc:
        logger.error(f"[EDPB_DOWNLOAD] filename={filename} | EXCEPTION error={exc}")
        return f"Failed to call the EDPB download API for '{filename}': {exc}"

    if resp.status_code != 200:
        logger.error(
            f"[EDPB_DOWNLOAD] filename={filename} | HTTP {resp.status_code} "
            f"body={resp.text[:500]}"
        )
        return (
            f"EDPB download API returned HTTP {resp.status_code} for "
            f"'{filename}': {resp.text[:500]}"
        )

    logger.info(f"[EDPB_DOWNLOAD] filename={filename} | HTTP 200")
    return f"EDPB download API response for '{filename}':\n{resp.text[:2000]}"
