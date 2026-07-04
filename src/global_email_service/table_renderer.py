"""Row dicts -> column/cell/color data for Jinja email templates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .colors import RowStyle, resolve_row_style
from .templating import render_html_template, render_text_template

# Never shown in the email table (internal styling / pipeline ordering).
_LOW_SIGNAL_KEYS = frozenset({"color", "row_color", "sequence_order"})

DEFAULT_SEGMENT_COLUMNS: List[str] = [
    "trade_date",
    "segment_code",
    "segment_name",
    "segment_status",
    "current_process",
    "current_phase",
    "process_id",
    "skip_category",
    "skip_reason",
    "started_at",
    "completed_at",
]

_SEVERITY_RANK = {
    "FAILED": 0, "FAIL": 0, "ERROR": 0, "CRITICAL": 0, "CBOS_ERROR": 0,
    "SKIPPED": 1, "SKIP": 1, "TIMEOUT": 1, "WARNING": 1, "WARN": 1,
    "AGENT_RESTART": 1, "MANUAL_SKIP": 1,
    "IN_PROGRESS": 2, "RUNNING": 2, "PENDING": 2, "INFO": 2, "BLOCKED": 2,
    "COMPLETED": 3, "COMPLETE": 3, "SUCCESS": 3, "SUCCEEDED": 3, "OK": 3, "DONE": 3,
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
    columns: Optional[Sequence[str]],
    rows: Sequence[dict],
) -> List[str]:
    if columns is not None:
        return [c for c in columns if c not in _LOW_SIGNAL_KEYS]
    return derive_columns(rows)


def derive_columns(rows: Sequence[dict]) -> List[str]:
    """First-seen key order; segment rows use DEFAULT_SEGMENT_COLUMNS as base."""
    seen: Dict[str, None] = {}
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
    return column.replace("_", " ").strip().title()


def _format_scalar(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def format_cell_value(value: Any) -> str:
    if isinstance(value, dict):
        if not value:
            return "—"
        parts = [f"{k}: {_format_scalar(v)}" for k, v in value.items()]
        return "; ".join(parts)
    if isinstance(value, (list, tuple)):
        if not value:
            return "—"
        return ", ".join(
            format_cell_value(item) if isinstance(item, dict) else _format_scalar(item)
            for item in value
        )
    return _format_scalar(value)


def resolve_severity(
    rows: Sequence[dict],
    color_overrides: Optional[Dict[str, tuple]] = None,
) -> RowStyle:
    worst_rank = 4
    worst_style: Optional[RowStyle] = None
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
    color_overrides: Optional[Dict[str, tuple]] = None,
) -> List[Dict[str, Any]]:
    table_rows = []
    for row in rows:
        style = resolve_row_style(row, color_overrides)
        table_rows.append({
            "background": style.background,
            "text_color": style.text_color,
            "cells": [format_cell_value(row.get(c)) for c in columns],
        })
    return table_rows


def render_html_table(
    rows: Sequence[dict],
    columns: Optional[Sequence[str]] = None,
    color_overrides: Optional[Dict[str, tuple]] = None,
) -> str:
    cols = _normalize_columns(columns, rows)
    return render_html_template(
        columns=[_prettify_header(c) for c in cols],
        table_rows=_build_table_rows(rows, cols, color_overrides),
        title=None, summary=None, severity=None,
        generated_at=_now_str(),
    )


def render_text_table(
    rows: Sequence[dict],
    columns: Optional[Sequence[str]] = None,
    color_overrides: Optional[Dict[str, tuple]] = None,
) -> str:
    cols = _normalize_columns(columns, rows)
    header = " | ".join(["ALERT"] + [_prettify_header(c) for c in cols])
    lines = [header, "-" * len(header)]
    for row in rows:
        style = resolve_row_style(row, color_overrides)
        values = [style.label] + [format_cell_value(row.get(c)) for c in cols]
        lines.append(" | ".join(values))
    return "\n".join(lines)


def _now_str() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(sep=" ", timespec="seconds")


def render_email_body(
    rows: Sequence[dict],
    *,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    columns: Optional[Sequence[str]] = None,
    color_overrides: Optional[Dict[str, tuple]] = None,
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
