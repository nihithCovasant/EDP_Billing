"""
EDP file-download chat tools that operate on MULTIPLE segments or dates in
one request — download_file (edpb_download.py) only takes one identifier +
one trade_date per call. This file sequentially calls the same underlying
EDPB download API for each segment/date, one HTTP call per target (the
downstream EDPB service has no native batch endpoint — see edpb_download.py's
module docstring), and returns one combined report.

Deliberately duplicates edpb_download.py's config/alias/date-normalization
helpers rather than importing them — same decoupling convention as every
other file in this directory (see edp_versions.py's module docstring for
the full rationale: the tool registry loads each file via
spec_from_file_location, not normal package imports).

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from cams_otel_lib import Logger as logger
from langchain_core.tools import tool

from src.config.agent_config import get_secrets, load_agent_config

_DEFAULT_API_BASE_URL = "http://localhost:7000"
_DEFAULT_TIMEOUT_SECONDS = 180.0
_CONNECT_RETRY_ATTEMPTS = 3
_CONNECT_RETRY_BACKOFF_SECONDS = 2.0
_MAX_DATE_RANGE_DAYS = 31  # sanity cap -- avoid an accidental thousand-call request

_cached_edpb_config: dict[str, Any] | None = None

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%B %d, %Y",
    "%b %d, %Y",
)
_ORDINAL_SUFFIX_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)

_CODE_ALIASES: dict[str, str] = {
    "EQ": "EQ",
    "CASH": "EQ",
    "EQUITY": "EQ",
    "DR": "DR",
    "F&O": "DR",
    "FO": "DR",
    "FNO": "DR",
    "DERIVATIVES": "DR",
    "CUR": "CUR",
    "CD": "CUR",
    "CURRENCY": "CUR",
    "SLB": "SLB",
    "NCDEX": "NCDEX",
    "NCDEXPHY": "NCDEXPHY",
    "NCDEX PHY": "NCDEXPHY",
    "NCDEX PHYSICAL": "NCDEXPHY",
    "MCX": "MCX",
    "MCXPHY": "MCXPHY",
    "MCX PHY": "MCXPHY",
    "MCX PHYSICAL": "MCXPHY",
    "NSECOM": "NSECOM",
    "NSE COMMODITY": "NSECOM",
    "COMMODITY": "NSECOM",
    "COLVAL": "COLVAL",
    "COLLATERAL VALUATION": "COLVAL",
    "COLALLOC": "COLALLOC",
    "COLLATERAL ALLOCATION": "COLALLOC",
    "MTFFT": "MTFFT",
    "MTF FUND TRANSFER": "MTFFT",
    "MTF": "MTFFT",
    "DMRPT": "DMRPT",
    "DAILY MARGIN REPORTING": "DMRPT",
    "DMSTMT": "DMSTMT",
    "DAILY MARGIN STATEMENTS": "DMSTMT",
}

ALL_SEGMENT_CODES = ("EQ", "DR", "CUR", "SLB", "NCDEX", "NCDEXPHY", "MCX", "MCXPHY", "NSECOM")


def _resolve_code(identifier: str) -> str | None:
    return _CODE_ALIASES.get(identifier.strip().upper())


def _get_edpb_config() -> dict[str, Any]:
    global _cached_edpb_config
    if _cached_edpb_config is None:
        secrets = get_secrets("default", load_agent_config())
        _cached_edpb_config = secrets.get("edpb_download", {})
    return _cached_edpb_config


def _config_value(env_name: str, config_key: str, default: str) -> str:
    env_value = os.getenv(env_name)
    if env_value:
        return env_value
    return _get_edpb_config().get(config_key, default)


def _today() -> str:
    return date.today().isoformat()


def _normalize_date(raw: str) -> str:
    cleaned = _ORDINAL_SUFFIX_RE.sub("", raw.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


async def _download_one(code: str, trade_date: str) -> tuple[bool, str]:
    """Returns (success, one-line report). Same connection-retry behavior as
    download_file: only httpx.ConnectError is retried, never a timeout."""
    base_url = _config_value("EDPB_DOWNLOAD_API_URL", "api_url", _DEFAULT_API_BASE_URL).rstrip("/")
    api_url = f"{base_url}/edpb/{code.lower()}/download"
    payload = {"trade_date": trade_date}
    headers = {"Content-Type": "application/json"}
    timeout_seconds = float(os.getenv("EDPB_DOWNLOAD_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS)))

    resp = None
    last_connect_error: Exception | None = None
    for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(api_url, json=payload, headers=headers)
            break
        except httpx.ConnectError as exc:
            last_connect_error = exc
            if attempt < _CONNECT_RETRY_ATTEMPTS:
                await asyncio.sleep(_CONNECT_RETRY_BACKOFF_SECONDS * attempt)
        except httpx.TimeoutException as exc:
            logger.error(f"[EDPB_DOWNLOAD_BULK] code={code} date={trade_date} | TIMEOUT: {exc}")
            return False, f"❌ **{code}** ({trade_date}) — timed out after {timeout_seconds:.0f}s"
        except Exception as exc:
            logger.error(f"[EDPB_DOWNLOAD_BULK] code={code} date={trade_date} | EXCEPTION: {exc}")
            return False, f"❌ **{code}** ({trade_date}) — {exc}"

    if resp is None:
        return (
            False,
            f"❌ **{code}** ({trade_date}) — connection failed after "
            f"{_CONNECT_RETRY_ATTEMPTS} attempts: {last_connect_error}",
        )

    if resp.status_code != 200:
        return False, f"❌ **{code}** ({trade_date}) — HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        data = resp.json()
    except Exception:
        return True, f"✅ **{code}** ({trade_date}) — status unknown (non-JSON response)"

    status = data.get("status", "unknown")
    ok = status in ("success", "SUCCESS")
    icon = "✅" if ok else "❌"
    detail = data.get("message") or data.get("reason") or ""
    return ok, f"{icon} **{code}** ({trade_date}) — {status}" + (f": {detail}" if detail else "")


@tool
async def download_edp_files_bulk(identifiers: list[str], trade_date: str | None = None) -> str:
    """
    Download EDPB files for SEVERAL segments/processes on the SAME trade
    date, in one request. Use this when the user asks to download files for
    multiple segments at once (e.g. "download EQ, DR, and CUR for today") or
    "download all segments" (pass every real trade segment code). For a
    single segment, prefer download_file instead.

    `identifiers` is a list of segment/process codes or common names.
    `trade_date` is optional (any date phrasing) — defaults to today if
    omitted. Each identifier is downloaded sequentially (the EDPB service
    has no native batch endpoint) and reported individually — a failure on
    one identifier does not stop the rest from being attempted.
    """
    if not identifiers:
        return "Please tell me which segments/processes to download."

    resolved_date = _normalize_date(trade_date) if trade_date else _today()

    codes = []
    unresolved = []
    for ident in identifiers:
        code = _resolve_code(ident)
        if code:
            codes.append(code)
        else:
            unresolved.append(ident)

    lines = [f"### 📥 Bulk download — {resolved_date}", ""]
    success_count = 0
    for code in codes:
        ok, line = await _download_one(code, resolved_date)
        lines.append(f"- {line}")
        if ok:
            success_count += 1

    if unresolved:
        lines.append(f"- ⚠️ Not recognized, skipped: {', '.join(unresolved)}")

    lines.insert(2, f"**{success_count}/{len(codes)}** succeeded.")
    return "\n".join(lines)


@tool
async def download_edp_files_date_range(identifier: str, start_date: str, end_date: str) -> str:
    """
    Download EDPB files for ONE segment/process across a RANGE of trade
    dates, in one request. Use this when the user asks to download files
    for a segment across several days (e.g. "download MCX files from 1st
    July to 5th July 2026"). For a single date, prefer download_file.

    `identifier` is the segment/process code or common name. `start_date`
    and `end_date` are both required (any date phrasing) and inclusive.
    Capped at 31 days per request to avoid an accidentally huge range —
    ask the user to narrow it if they want more.
    """
    code = _resolve_code(identifier)
    if not code:
        return (
            f'I don\'t recognize "{identifier}" as a segment or post-trade process. Try a code '
            f"like EQ, DR, CUR, SLB, NCDEX, MCX, NSECOM, COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT, "
            f"or a common name like Cash, F&O, CD, Collateral Valuation."
        )

    start = datetime.strptime(_normalize_date(start_date), "%Y-%m-%d").date()
    end = datetime.strptime(_normalize_date(end_date), "%Y-%m-%d").date()
    if end < start:
        return f"❌ end_date ({end}) is before start_date ({start})."

    span_days = (end - start).days + 1
    if span_days > _MAX_DATE_RANGE_DAYS:
        return (
            f"❌ That's a {span_days}-day range — capped at {_MAX_DATE_RANGE_DAYS} days per request. "
            f"Please narrow the range."
        )

    lines = [f"### 📥 **{code}** — {start} to {end}", ""]
    success_count = 0
    d = start
    total = 0
    while d <= end:
        total += 1
        ok, line = await _download_one(code, d.isoformat())
        lines.append(f"- {line}")
        if ok:
            success_count += 1
        d += timedelta(days=1)

    lines.insert(2, f"**{success_count}/{total}** succeeded.")
    return "\n".join(lines)
