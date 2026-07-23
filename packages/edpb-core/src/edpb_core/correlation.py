"""Correlation-id conventions (wayfinder ticket 11).

One id per (segment, trade date) run, minted by whoever starts the journey
(the EDP_Billing engine in the engine-owned saga; the bot for standalone
runs) and carried by everyone else:

  engine --X-Request-ID--> bot --manifest.correlation_id--> uploader
  (logs)                   (logs)                           (logs + audit rows)
"""

from __future__ import annotations

import uuid
from datetime import date

CORRELATION_HEADER = "X-Request-ID"


def mint_run_id(segment: str, trade_date: str | date) -> str:
    """The engine's per-run id shape: edp-{segment}-{iso date}-{8 hex}."""
    iso = trade_date if isinstance(trade_date, str) else trade_date.isoformat()
    return f"edp-{segment.lower()}-{iso}-{uuid.uuid4().hex[:8]}"
