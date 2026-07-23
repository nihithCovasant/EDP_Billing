"""
EDPB file download tool for the chat agent.

Lets a user ask in chat (e.g. "download the files for MCX for trade date
10th July 2026") and the agent calls the per-segment/process EDPB download
API: POST {base_url}/edpb/{segment_or_process_code}/download with body
{"trade_date": "YYYY-MM-DD"}. The segment/process code is a path
placeholder — this works for any of the real trade segments (EQ, DR, CUR,
SLB, NCDEX, NCDEXPHY, MCX, MCXPHY, NSECOM) and post-trade processes
(COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT), not just MCX.

Config (api_url) lives in agent_config.json ->
agent_config.secrets.edpb_download, same place as litellm/database/etc
secrets (see README.md's config split: API keys in .env, everything else in
agent_config.json). The matching EDPB_DOWNLOAD_API_URL env var, if set,
overrides the config file value — useful for local one-off testing without
editing the committed config. No API key/auth header is sent — the
downstream EDPB service doesn't require one.

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed. Completely independent of src/agent/edp/** (the EDP
billing wake loop/state machine); this file is never imported from there.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime
from typing import Any, Dict, Optional

import httpx
from langchain_core.tools import tool
from cams_otel_lib import Logger as logger

from src.config.agent_config import get_secrets, load_agent_config

# Auto-retry ONLY applies to connection-establishment failures (DNS/refused/
# unreachable) -- i.e. the request never actually reached the server, so
# retrying is safe. A read-timeout (server accepted the request but didn't
# respond in time) is deliberately NEVER auto-retried here: per the
# _DEFAULT_TIMEOUT_SECONDS comment above, the server-side download keeps
# running after a client timeout, so blindly retrying on timeout risks
# kicking off a SECOND concurrent server-side download while the first one
# may still be in flight.
_CONNECT_RETRY_ATTEMPTS = 3
_CONNECT_RETRY_BACKOFF_SECONDS = 2.0

# Placeholder — used only if neither agent_config.json nor an EDPB_* env
# var provides a real one.
_DEFAULT_API_BASE_URL = "http://localhost:7000"

# A single download call can trigger multiple chunked file downloads
# server-side (trade/position/product-master, ...), which can comfortably
# exceed a "normal" API timeout. A client-side timeout here does NOT stop
# the server-side download -- it just means we give up waiting and report
# failure even though the download completes anyway moments later -- so
# this is deliberately generous. Overridable via EDPB_DOWNLOAD_TIMEOUT_SECONDS
# for slower environments without editing code.
_DEFAULT_TIMEOUT_SECONDS = 180.0

_cached_edpb_config: Optional[Dict[str, Any]] = None

_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
    "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y",
)
_ORDINAL_SUFFIX_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)

# Same segment/process code <-> common-name aliases as src/tools/edp_status.py
# (kept local rather than imported, for the same decoupling reason as the
# rest of this file — these rarely change).
_CODE_ALIASES: Dict[str, str] = {
    "EQ": "EQ", "CASH": "EQ", "EQUITY": "EQ",
    "DR": "DR", "F&O": "DR", "FO": "DR", "FNO": "DR", "DERIVATIVES": "DR",
    "CUR": "CUR", "CD": "CUR", "CURRENCY": "CUR",
    "SLB": "SLB",
    "NCDEX": "NCDEX",
    "NCDEXPHY": "NCDEXPHY", "NCDEX PHY": "NCDEXPHY", "NCDEX PHYSICAL": "NCDEXPHY",
    "MCX": "MCX",
    "MCXPHY": "MCXPHY", "MCX PHY": "MCXPHY", "MCX PHYSICAL": "MCXPHY",
    "NSECOM": "NSECOM", "NSE COMMODITY": "NSECOM", "COMMODITY": "NSECOM",
    "COLVAL": "COLVAL", "COLLATERAL VALUATION": "COLVAL",
    "COLALLOC": "COLALLOC", "COLLATERAL ALLOCATION": "COLALLOC",
    "MTFFT": "MTFFT", "MTF FUND TRANSFER": "MTFFT", "MTF": "MTFFT",
    "DMRPT": "DMRPT", "DAILY MARGIN REPORTING": "DMRPT",
    "DMSTMT": "DMSTMT", "DAILY MARGIN STATEMENTS": "DMSTMT",
}


def _resolve_code(identifier: str) -> Optional[str]:
    return _CODE_ALIASES.get(identifier.strip().upper())


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
async def download_file(identifier: str, trade_date: Optional[str] = None) -> str:
    """
    Download EDPB files for a trade segment or post-trade process on a
    given trade date.

    Use this whenever the user asks to download files, regardless of exact
    phrasing — e.g. "download the files for MCX", "download MCX files for
    trade date 10th July 2026", "get me the EQ download for today".

    `identifier` is REQUIRED — the segment/process code (EQ, DR, CUR, SLB,
    NCDEX, NCDEXPHY, MCX, MCXPHY, NSECOM, COLVAL, COLALLOC, MTFFT, DMRPT,
    DMSTMT) or a common name (Cash, F&O, CD, Collateral Valuation, ...).
    `trade_date` is optional — the date they mentioned (any format, e.g.
    "10th July 2026" or "2026-07-10"); if they don't mention one, today's
    date is used automatically.
    """
    code = _resolve_code(identifier)
    if not code:
        return (
            f'I don\'t recognize "{identifier}" as a segment or post-trade process. Try a code '
            f"like EQ, DR, CUR, SLB, NCDEX, MCX, NSECOM, COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT, "
            f"or a common name like Cash, F&O, CD, Collateral Valuation."
        )

    base_url = _config_value("EDPB_DOWNLOAD_API_URL", "api_url", _DEFAULT_API_BASE_URL).rstrip("/")
    resolved_trade_date = _normalize_date(trade_date) if trade_date else _today()

    api_url = f"{base_url}/edpb/{code.lower()}/download"
    payload = {"trade_date": resolved_trade_date}
    headers = {"Content-Type": "application/json"}
    timeout_seconds = float(os.getenv("EDPB_DOWNLOAD_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS)))

    logger.info(f"[EDPB_DOWNLOAD] code={code} trade_date={resolved_trade_date} | POST {api_url}")

    resp = None
    last_connect_error: Optional[Exception] = None
    for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(api_url, json=payload, headers=headers)
            break
        except httpx.ConnectError as exc:
            last_connect_error = exc
            logger.warning(
                f"[EDPB_DOWNLOAD] code={code} | connection attempt {attempt}/{_CONNECT_RETRY_ATTEMPTS} "
                f"failed (never reached the server, safe to retry): {exc}"
            )
            if attempt < _CONNECT_RETRY_ATTEMPTS:
                await asyncio.sleep(_CONNECT_RETRY_BACKOFF_SECONDS * attempt)
        except httpx.TimeoutException as exc:
            logger.error(
                f"[EDPB_DOWNLOAD] code={code} | TIMEOUT after {timeout_seconds}s error={exc} — "
                f"the server-side download may still complete even though this request gave up waiting; "
                f"NOT auto-retrying a timeout (unlike a connection failure) since the request may have "
                f"already reached the server"
            )
            return (
                f"Timed out waiting for the EDPB download API for **{code}** (trade_date={resolved_trade_date}) "
                f"after {timeout_seconds:.0f}s. The download may still be running/have completed on the server "
                f"side — re-check status before assuming it failed."
            )
        except Exception as exc:
            logger.error(f"[EDPB_DOWNLOAD] code={code} | EXCEPTION error={exc}")
            return f"Failed to call the EDPB download API for **{code}**: {exc}"

    if resp is None:
        logger.error(
            f"[EDPB_DOWNLOAD] code={code} | connection failed after {_CONNECT_RETRY_ATTEMPTS} attempts: "
            f"{last_connect_error}"
        )
        return (
            f"Could not reach the EDPB download API for **{code}** (trade_date={resolved_trade_date}) after "
            f"{_CONNECT_RETRY_ATTEMPTS} attempts — connection failed each time: {last_connect_error}"
        )

    if resp.status_code != 200:
        logger.error(
            f"[EDPB_DOWNLOAD] code={code} | HTTP {resp.status_code} body={resp.text[:500]}"
        )
        return (
            f"EDPB download API returned HTTP {resp.status_code} for "
            f"**{code}** (trade_date={resolved_trade_date}): {resp.text[:500]}"
        )

    logger.info(f"[EDPB_DOWNLOAD] code={code} | HTTP 200")

    try:
        data = resp.json()
    except Exception:
        return (
            f"EDPB download API response for **{code}** (trade_date={resolved_trade_date}):\n"
            f"{resp.text[:2000]}"
        )

    status = data.get("status", "unknown")
    icon = "✅" if status in ("success", "SUCCESS") else "❌"
    lines = [f"{icon} **{code}** ({resolved_trade_date}) — status: **{status}**"]
    if data.get("message"):
        lines.append(f"- **Message:** {data['message']}")
    if data.get("reason"):
        lines.append(f"- **Reason:** {data['reason']}")
    # Real EDPB download-API response shape: lists of downloaded filenames
    # grouped by file type (not a single file_name/file_path pair) plus the
    # directory they landed in. "missing" is deliberately not surfaced here
    # per product decision -- a partial file-type gap isn't reported as an
    # overall failure.
    for file_type in ("trade", "position", "product_master"):
        files = data.get(file_type)
        if files:
            lines.append(f"- **{file_type.replace('_', ' ').title()}:** {', '.join(files)}")
    if data.get("download_dir"):
        lines.append(f"- **Download dir:** {data['download_dir']}")
    if len(lines) == 1:
        lines.append(f"- **Raw response:** {data}")
    return "\n".join(lines)
