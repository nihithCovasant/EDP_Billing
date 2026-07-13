"""
EDPB script/file download tool for the chat agent.

Lets a user ask in chat (e.g. "download the script with script name
bse_edpb_script1 for date 10th July 2026") and the agent calls the EDPB
download API with the fixed member credentials plus that script name and
trade date.

Config (api_url/member_code/username/password) lives in agent_config.json
-> agent_config.secrets.edpb_download, same place as litellm/database/etc
secrets (see README.md's config split: API keys in .env, everything else in
agent_config.json). Matching EDPB_* env vars, if set, override the config
file value — useful for local one-off testing without editing the
committed config.

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed. Completely independent of src/agent/edp/** (the EDP
billing wake loop/state machine); this file is never imported from there.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Dict, Optional

import httpx
from langchain_core.tools import tool
from cams_otel_lib import Logger as logger

from src.config.agent_config import get_secrets, load_agent_config

# Placeholder — used only if neither agent_config.json nor an EDPB_* env
# var provides a real one.
_DEFAULT_API_URL = "http://localhost:9300/api/edpb/download"

_cached_edpb_config: Optional[Dict[str, Any]] = None

_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
    "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y",
)
_ORDINAL_SUFFIX_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)


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


def _normalize_date(raw: str) -> str:
    """
    Best-effort "YYYY-MM-DD" normalizer, so the tool still works if the LLM
    passes a natural-language date (e.g. "10th July 2026") instead of
    converting it itself. Falls back to the raw string unchanged if none of
    the known formats match.
    """
    cleaned = _ORDINAL_SUFFIX_RE.sub("", raw.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


@tool
async def download_file(script_name: str, trade_date: Optional[str] = None) -> str:
    """
    Download a script/file from the EDPB portal by name.

    Use this whenever the user asks to download a file/script, regardless of
    exact phrasing — e.g. "download file with filename <name>", "download
    the script with script name <name>", "get me <name>", "download
    <name> for date 10th July 2026". `script_name` is required — the exact
    file/script name the user mentioned, taken verbatim from their message.
    `trade_date` is optional — the date they mentioned (any format, e.g.
    "10th July 2026" or "2026-07-10"); if they don't mention one, today's
    date is used automatically.
    """
    api_url = _config_value("EDPB_DOWNLOAD_API_URL", "api_url", _DEFAULT_API_URL)
    resolved_trade_date = _normalize_date(trade_date) if trade_date else _today()

    payload = {
        "member_code": _config_value("EDPB_MEMBER_CODE", "member_code", "0446"),
        "username": _config_value("EDPB_USERNAME", "username", "0446"),
        "password": _config_value("EDPB_PASSWORD", "password", "your_password"),
        "script_name": script_name,
        "trade_date": resolved_trade_date,
    }

    logger.info(
        f"[EDPB_DOWNLOAD] script_name={script_name} trade_date={resolved_trade_date} "
        f"| POST {api_url}"
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(api_url, json=payload)
    except Exception as exc:
        logger.error(f"[EDPB_DOWNLOAD] script_name={script_name} | EXCEPTION error={exc}")
        return f"Failed to call the EDPB download API for '{script_name}': {exc}"

    if resp.status_code != 200:
        logger.error(
            f"[EDPB_DOWNLOAD] script_name={script_name} | HTTP {resp.status_code} "
            f"body={resp.text[:500]}"
        )
        return (
            f"EDPB download API returned HTTP {resp.status_code} for "
            f"'{script_name}' (trade_date={resolved_trade_date}): {resp.text[:500]}"
        )

    logger.info(f"[EDPB_DOWNLOAD] script_name={script_name} | HTTP 200")

    # Response shape (PortalDownloadResponse): status, file_name, file_path,
    # trade_date, captcha_attempts, message. Fall back to raw text if the
    # API's response shape ever changes underneath us.
    try:
        data = resp.json()
    except Exception:
        return (
            f"EDPB download API response for '{script_name}' (trade_date={resolved_trade_date}):\n"
            f"{resp.text[:2000]}"
        )

    status = data.get("status", "unknown")
    icon = "✅" if status == "success" else "❌"
    lines = [
        f"{icon} **{script_name}** ({resolved_trade_date}) — status: **{status}**",
        f"- **Message:** {data.get('message', '—')}",
    ]
    if data.get("file_name"):
        lines.append(f"- **File:** {data['file_name']}")
    if data.get("file_path"):
        lines.append(f"- **Path:** {data['file_path']}")
    if data.get("captcha_attempts") is not None:
        lines.append(f"- **Captcha attempts:** {data['captcha_attempts']}")
    return "\n".join(lines)
