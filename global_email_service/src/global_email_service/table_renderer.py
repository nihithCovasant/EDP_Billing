"""Row dicts -> column/cell/color data for Jinja email templates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .colors import RowStyle, resolve_row_style
from .templating import render_html_template, render_text_template

# This service's only current caller (EDP Billing) runs exclusively in IST —
# see from_name="EDP Billing Alerts" in config.py — so the footer timestamp
# is pinned here explicitly rather than left to datetime.astimezone()'s
# implicit OS-timezone behavior, matching the explicit IST conversion the
# EDP caller already applies to its own row-level started_at/completed_at.
_IST = ZoneInfo("Asia/Kolkata")

# Never shown in the email table (internal styling / pipeline ordering).
_LOW_SIGNAL_KEYS = frozenset({"color", "row_color", "sequence_order", "skip_category"})

DEFAULT_SEGMENT_COLUMNS: list[str] = [
    "trade_date",
    "segment_code",
    "segment_name",
    "segment_status",
    "current_process",
    "current_state",
    "process_id",
    "skip_reason",
    "started_at",
    "completed_at",
]

# Customer-facing column headers (internal field keys unchanged in payload).
_COLUMN_HEADERS: dict[str, str] = {
    "segment_status": "Segment Status",
    "skip_reason": "Remarks",
    "current_state": "Stage",
    "current_process": "Process",
    "process_id": "Process ID",
    "trade_date": "Trade Date",
    "segment_code": "Segment Code",
    "segment_name": "Segment Name",
    "started_at": "Started At",
    "completed_at": "Completed At",
}

_SEGMENT_STATUS_DISPLAY: dict[str, str] = {
    "COMPLETED": "Succeeded",
    "COMPLETE": "Succeeded",
    "SUCCESS": "Succeeded",
    "SUCCEEDED": "Succeeded",
    "OK": "Succeeded",
    "DONE": "Succeeded",
    "FAILED": "Failed",
    "FAIL": "Failed",
    "ERROR": "Failed",
    "CRITICAL": "Failed",
    "SKIPPED": "Skipped",
    "SKIP": "Skipped",
    "PENDING": "Pending",
    "IN_PROGRESS": "In Progress",
    "RUNNING": "In Progress",
    "BLOCKED": "Blocked",
    "TIMEOUT": "Timed Out",
    "WARNING": "Warning",
    "WARN": "Warning",
}

_PROCESS_DISPLAY: dict[str, str] = {
    "FILEUPLOAD": "File Upload",
    "BILLPOSTING": "Bill Posting",
    "RECON": "Reconciliation",
    "CONTRACTNOTEGENERATION": "Contract Note",
    "CONTRACT_NOTE": "Contract Note",
    "BEGINFILEUPLOAD": "Begin File Upload",
    "CollateralValuation": "Collateral Valuation",
    "CollateralAllocation": "Collateral Allocation",
    "FundTransfer": "Fund Transfer",
    "EARLYPAYIN": "Early Pay-in",
    "WEEKLYAUTOCLOSURE": "Weekly Auto Closure",
}

# Customer-facing pipeline stage (current_state -> readable label).
_STAGE_DISPLAY: dict[str, str] = {
    "INIT": "Good to Go",
    "WAITING_FOR_FILE_UPLOAD": "Good to Go",
    "WAITING_FOR_GTG": "Good to Go",
    "TRIGGERED": "Triggering",
    "WAITING_FOR_BILLPOSTING": "Completion",
    "WAITING_FOR_RECON": "Completion",
    "WAITING_FOR_CONTRACT_NOTE_GENERATION": "Completion",
    "WAITING_FOR_COMPLETION": "Completion",
}

# Fallback when phase is missing but current_process is set.
_PROCESS_STAGE_DISPLAY: dict[str, str] = {
    "FILEUPLOAD": "Good to Go",
    "BEGINFILEUPLOAD": "Good to Go",
    "BILLPOSTING": "Completion",
    "RECON": "Completion",
    "CONTRACTNOTEGENERATION": "Completion",
    "CONTRACT_NOTE": "Completion",
    "CollateralValuation": "Good to Go",
    "CollateralAllocation": "Good to Go",
    "FundTransfer": "Good to Go",
    "EARLYPAYIN": "Completion",
    "WEEKLYAUTOCLOSURE": "Completion",
}

_SEVERITY_RANK = {
    "FAILED": 0,
    "FAIL": 0,
    "ERROR": 0,
    "CRITICAL": 0,
    "CBOS_ERROR": 0,
    "SKIPPED": 1,
    "SKIP": 1,
    "TIMEOUT": 1,
    "WARNING": 1,
    "WARN": 1,
    "AGENT_RESTART": 1,
    "MANUAL_SKIP": 1,
    "IN_PROGRESS": 2,
    "RUNNING": 2,
    "PENDING": 2,
    "INFO": 2,
    "BLOCKED": 2,
    "COMPLETED": 3,
    "COMPLETE": 3,
    "SUCCESS": 3,
    "SUCCEEDED": 3,
    "OK": 3,
    "DONE": 3,
}
_SEVERITY_BANNER_TEXT = {
    0: "ACTION REQUIRED — one or more records FAILED",
    1: "REVIEW REQUIRED — one or more records were SKIPPED / timed out",
    2: "IN PROGRESS — one or more records still pending / running",
    3: "ALL CLEAR — all records completed successfully",
}


def _looks_like_segment_row(row: dict) -> bool:
    return "segment_code" in row


def _normalize_columns(
    columns: Sequence[str] | None,
    rows: Sequence[dict],
) -> list[str]:
    if columns is not None:
        return [c for c in columns if c not in _LOW_SIGNAL_KEYS]
    return derive_columns(rows)


def derive_columns(rows: Sequence[dict]) -> list[str]:
    """First-seen key order; segment rows use DEFAULT_SEGMENT_COLUMNS as base."""
    seen: dict[str, None] = {}
    for row in rows:
        for key in row.keys():
            if key in _LOW_SIGNAL_KEYS:
                continue
            seen.setdefault(key, None)
    discovered = list(seen.keys())

    if any(_looks_like_segment_row(row) for row in rows):
        extras = [c for c in discovered if c not in DEFAULT_SEGMENT_COLUMNS]
        return list(DEFAULT_SEGMENT_COLUMNS) + extras

    return discovered


def _prettify_header(column: str) -> str:
    return _COLUMN_HEADERS.get(column, column.replace("_", " ").strip().title())


def _display_token(value: str, mapping: dict[str, str]) -> str:
    return mapping.get(value.strip().upper(), mapping.get(value.strip(), value))


def _format_stage(state: Any, row: dict | None = None) -> str:
    if state is not None and str(state).strip():
        key = str(state).strip().upper()
        if key in _STAGE_DISPLAY:
            return _STAGE_DISPLAY[key]
    if row:
        process = row.get("current_process")
        if process:
            proc_key = str(process).strip()
            stage = _PROCESS_STAGE_DISPLAY.get(
                proc_key,
                _PROCESS_STAGE_DISPLAY.get(proc_key.upper()),
            )
            if stage:
                return stage
    if state is None or state == "":
        return "—"
    return str(state).replace("_", " ").title()


def _format_segment_cell(column: str, value: Any, row: dict | None = None) -> str:
    if column == "current_state":
        return _format_stage(value, row)

    if value is None or value == "":
        return "—"

    if column == "segment_status":
        return _display_token(str(value), _SEGMENT_STATUS_DISPLAY)

    if column == "current_process":
        key = str(value).strip()
        return _PROCESS_DISPLAY.get(key, _PROCESS_DISPLAY.get(key.upper(), key.replace("_", " ").title()))

    return _format_scalar(value)


def _format_scalar(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def format_cell_value(value: Any, *, column: str | None = None, row: dict | None = None) -> str:
    if column in _COLUMN_HEADERS and column not in ("trade_date", "started_at", "completed_at"):
        if not isinstance(value, (dict, list, tuple)):
            return _format_segment_cell(column, value, row=row)

    if isinstance(value, dict):
        if not value:
            return "—"
        parts = [f"{k}: {_format_scalar(v)}" for k, v in value.items()]
        return "; ".join(parts)
    if isinstance(value, (list, tuple)):
        if not value:
            return "—"
        return ", ".join(format_cell_value(item) if isinstance(item, dict) else _format_scalar(item) for item in value)
    return _format_scalar(value)


def resolve_severity(
    rows: Sequence[dict],
    color_overrides: dict[str, tuple] | None = None,
) -> RowStyle:
    worst_rank = 4
    worst_style: RowStyle | None = None
    for row in rows:
        style = resolve_row_style(row, color_overrides)
        rank = _SEVERITY_RANK.get(style.label, 4)
        if rank < worst_rank:
            worst_rank = rank
            worst_style = style

    if worst_style is None:
        return RowStyle(background="#e2e3e5", text_color="#41464b", label="STATUS UNKNOWN")

    banner_text = _SEVERITY_BANNER_TEXT.get(worst_rank, worst_style.label)
    return RowStyle(background=worst_style.background, text_color=worst_style.text_color, label=banner_text)


def _build_table_rows(
    rows: Sequence[dict],
    columns: Sequence[str],
    color_overrides: dict[str, tuple] | None = None,
) -> list[dict[str, Any]]:
    table_rows = []
    for row in rows:
        style = resolve_row_style(row, color_overrides)
        table_rows.append(
            {
                "background": style.background,
                "text_color": style.text_color,
                "cells": [format_cell_value(row.get(c), column=c, row=row) for c in columns],
            }
        )
    return table_rows


def render_html_table(
    rows: Sequence[dict],
    columns: Sequence[str] | None = None,
    color_overrides: dict[str, tuple] | None = None,
) -> str:
    cols = _normalize_columns(columns, rows)
    return render_html_template(
        columns=[_prettify_header(c) for c in cols],
        table_rows=_build_table_rows(rows, cols, color_overrides),
        title=None,
        summary=None,
        severity=None,
        generated_at=_now_str(),
    )


def render_text_table(
    rows: Sequence[dict],
    columns: Sequence[str] | None = None,
    color_overrides: dict[str, tuple] | None = None,
) -> str:
    cols = _normalize_columns(columns, rows)
    header = " | ".join(["ALERT"] + [_prettify_header(c) for c in cols])
    lines = [header, "-" * len(header)]
    for row in rows:
        style = resolve_row_style(row, color_overrides)
        values = [style.label] + [format_cell_value(row.get(c), column=c, row=row) for c in cols]
        lines.append(" | ".join(values))
    return "\n".join(lines)


def _now_str() -> str:
    """Short IST form ('YYYY-MM-DD HH:MM:SS IST') — no microseconds, no
    numeric offset, matching the row-level timestamp style rather than
    the previous raw datetime.isoformat() (e.g. '...+05:30')."""
    return datetime.now(UTC).astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def render_email_body(
    rows: Sequence[dict],
    *,
    title: str | None = None,
    summary: str | None = None,
    columns: Sequence[str] | None = None,
    color_overrides: dict[str, tuple] | None = None,
) -> tuple[str, str]:
    cols = _normalize_columns(columns, rows)
    pretty_cols = [_prettify_header(c) for c in cols]
    table_rows = _build_table_rows(rows, cols, color_overrides)
    severity = resolve_severity(rows, color_overrides)
    generated_at = _now_str()

    html_body = render_html_template(
        title=title,
        summary=summary,
        severity=severity,
        columns=pretty_cols,
        table_rows=table_rows,
        generated_at=generated_at,
    )

    text_table = render_text_table(rows, cols, color_overrides)
    text_body = render_text_template(
        title=title,
        summary=summary,
        severity=severity,
        text_table=text_table,
        generated_at=generated_at,
    )

    return html_body, text_body
