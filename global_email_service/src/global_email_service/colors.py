"""Status/severity -> row background color."""

from __future__ import annotations

from dataclasses import dataclass

STATUS_LIKE_FIELDS = (
    "severity",
    "alert_level",
    "segment_status",
    "status",
    "state",
)


@dataclass(frozen=True)
class RowStyle:
    background: str
    text_color: str
    label: str


_RED = ("#f8d7da", "#842029")
_YELLOW = ("#fff3cd", "#664d03")
_BLUE = ("#cfe2ff", "#084298")
_GREEN = ("#d1e7dd", "#0f5132")
_GREY = ("#e2e3e5", "#41464b")

DEFAULT_STATUS_COLOR_MAP: dict[str, tuple[str, str]] = {
    "FAILED": _RED,
    "FAIL": _RED,
    "ERROR": _RED,
    "CRITICAL": _RED,
    "CBOS_ERROR": _RED,
    "SKIPPED": _YELLOW,
    "SKIP": _YELLOW,
    "TIMEOUT": _YELLOW,
    "WARNING": _YELLOW,
    "WARN": _YELLOW,
    "AGENT_RESTART": _YELLOW,
    "MANUAL_SKIP": _YELLOW,
    "IN_PROGRESS": _BLUE,
    "RUNNING": _BLUE,
    "PENDING": _BLUE,
    "INFO": _BLUE,
    "BLOCKED": _BLUE,
    "COMPLETED": _GREEN,
    "COMPLETE": _GREEN,
    "SUCCESS": _GREEN,
    "SUCCEEDED": _GREEN,
    "OK": _GREEN,
    "DONE": _GREEN,
}


def resolve_row_style(
    row: dict,
    color_overrides: dict[str, tuple[str, str]] | None = None,
) -> RowStyle:
    color_map = {**DEFAULT_STATUS_COLOR_MAP, **(color_overrides or {})}

    explicit_color = row.get("color") or row.get("row_color")
    if explicit_color:
        label = (
            str(
                row.get("severity") or row.get("alert_level") or row.get("segment_status") or row.get("status") or ""
            ).upper()
            or "ALERT"
        )
        return RowStyle(background=str(explicit_color), text_color="#212529", label=label)

    for field_name in STATUS_LIKE_FIELDS:
        value = row.get(field_name)
        if not value:
            continue
        key = str(value).strip().upper()
        if key in color_map:
            bg, fg = color_map[key]
            return RowStyle(background=bg, text_color=fg, label=key)
        bg, fg = _GREY
        return RowStyle(background=bg, text_color=fg, label=key)

    bg, fg = _GREY
    return RowStyle(background=bg, text_color=fg, label="—")
