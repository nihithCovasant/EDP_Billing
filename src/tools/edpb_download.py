"""
EDPB file download tool for the chat agent.

Lets a user ask in chat (e.g. "download file with filename VN_09072026.txt")
and the agent calls the EDPB download API with the fixed portal/credentials
plus that filename. The real API contract (URL + exact response shape) is
still TBD with the EDPB team, so this deliberately just wires up the
request/response plumbing for now — refine parsing once the real API is
confirmed.

Config (api_url/portal/member_code/user_id/password/edpb_action) lives in
agent_config.json -> agent_config.secrets.edpb_download, same place as
litellm/database/etc secrets (see README.md's config split: API keys in
.env, everything else in agent_config.json). Matching EDPB_* env vars, if
set, override the config file value — useful for local one-off testing
without editing the committed config.

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed. Completely independent of src/agent/edp/** (the EDP
billing wake loop/state machine); this file is never imported from there.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, Optional

import httpx
from langchain_core.tools import tool
from cams_otel_lib import Logger as logger

from src.config.agent_config import get_secrets, load_agent_config

# Placeholder — the real EDPB download endpoint URL isn't finalized yet.
_DEFAULT_API_URL = "http://localhost:9300/api/edpb/download"

_cached_edpb_config: Optional[Dict[str, Any]] = None


def _get_edpb_config() -> Dict[str, Any]:
    """agent_config.json -> agent_config.secrets.edpb_download, loaded once and cached."""
    global _cached_edpb_config
    if _cached_edpb_config is None:
        secrets = get_secrets("default", load_agent_config())
        _cached_edpb_config = secrets.get("edpb_download", {})
    return _cached_edpb_config


def _config_value(env_name: str, config_key: str, default: str) -> str:
    """EDPB_* env var (if set) > agent_config.json value > hardcoded default."""
    env_value = os.getenv(env_name)
    if env_value:
        return env_value
    return _get_edpb_config().get(config_key, default)


def _today() -> str:
    return date.today().isoformat()


@tool
async def download_file(filename: str, trade_date: Optional[str] = None) -> str:
    """
    Download a file (also called a "script" in EDPB terminology) from the
    EDPB portal by name.

    Use this whenever the user asks to download a file/script, regardless of
    exact phrasing — e.g. "download file with filename <name>", "download
    the script with script name <name>", "get me <name>", etc. `filename` is
    required — the exact file/script name the user mentioned, taken verbatim
    from their message. `trade_date` is optional, format YYYY-MM-DD — if the
    user doesn't mention one, today's date is used automatically.
    """
    api_url = _config_value("EDPB_DOWNLOAD_API_URL", "api_url", _DEFAULT_API_URL)
    resolved_trade_date = trade_date or _today()

    payload = {
        "portal": _config_value("EDPB_PORTAL", "portal", "bse_edpb"),
        "member_code": _config_value("EDPB_MEMBER_CODE", "member_code", "0446"),
        "user_id": _config_value("EDPB_USER_ID", "user_id", "0446"),
        "password": _config_value("EDPB_PASSWORD", "password", "your_password"),
        "trade_date": resolved_trade_date,
        "edpb_action": _config_value("EDPB_ACTION", "edpb_action", "vn"),
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
