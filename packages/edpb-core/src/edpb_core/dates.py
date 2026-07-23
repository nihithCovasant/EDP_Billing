"""Date-format conventions (BATCH_HANDOFF_CONTRACT.md):

  - FOLDER names use DD-MM-YYYY  ({FILE_ROOT}/{20-07-2026}/{MCX}/...)
  - MANIFESTS and service APIs use ISO YYYY-MM-DD
  - CBOS's TRADEDATE/FILTER2 fields use ISO YYYY-MM-DD
  - CBOS's post-trade MARGINDATE uses DD-Mon-YYYY (e.g. "29-Jun-2026")

One converter per direction, so no service hand-rolls strftime patterns.
"""

from __future__ import annotations

from datetime import date, datetime

FOLDER_DATE_FORMAT = "%d-%m-%Y"
CBOS_MARGINDATE_FORMAT = "%d-%b-%Y"


def folder_date_to_iso(folder_date: str) -> str:
    """'20-07-2026' -> '2026-07-20'."""
    return datetime.strptime(folder_date, FOLDER_DATE_FORMAT).strftime("%Y-%m-%d")


def iso_to_folder_date(iso_date: str | date) -> str:
    """'2026-07-20' (or a date) -> '20-07-2026'."""
    d = date.fromisoformat(iso_date) if isinstance(iso_date, str) else iso_date
    return d.strftime(FOLDER_DATE_FORMAT)


def to_cbos_margindate(iso_date: str | date) -> str:
    """'2026-06-29' (or a date) -> '29-Jun-2026' (post-trade trigger payloads)."""
    d = date.fromisoformat(iso_date) if isinstance(iso_date, str) else iso_date
    return d.strftime(CBOS_MARGINDATE_FORMAT)
