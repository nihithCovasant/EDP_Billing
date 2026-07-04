"""
Fixed constants for the EDP segment pipeline.

Per the EDP Billing segment flow, the agent processes 7 segments sequentially
— CASH -> F&O -> CD -> SLBM -> MCX -> NCDEX -> MTF — each running through the
identical generic 7-step pipeline (holiday check -> get-or-reserve process ID
-> file upload poll -> single trigger -> bill posting / recon / contract note
polls). MTF is not special-cased — it is just the 7th segment in sequence,
driven through the exact same pipeline as every other segment.
"""

from __future__ import annotations

from datetime import timedelta

# Fixed processing order for the 7 trade segments. This used to be a
# per-day config field (workflow_json.segments[].sequence_order) mirrored
# onto a segment_execution DB column; it is now a code constant since the
# regulatory sequence does not change day to day. Changing the order
# requires a code change.
SEGMENT_ORDER: tuple[str, ...] = (
    "EQ", "DR", "CUR", "SL", "MCX", "NCDEX", "MTF",
)

# Human display labels — also used to be a per-day config field
# (workflow_json.segments[].segment_name) mirrored onto a segment_execution
# DB column; folded into a code constant for the same reason as SEGMENT_ORDER.
SEGMENT_NAMES: dict[str, str] = {
    "EQ": "Cash",
    "DR": "F&O",
    "CUR": "CD",
    "SL": "SLBM",
    "MCX": "MCX",
    "NCDEX": "NCDEX",
    "MTF": "MTF",
}

# Heartbeat staleness threshold — a segment is considered STALE (for display
# purposes only, not persisted) if it's IN_PROGRESS but hasn't had a
# heartbeat in this long. Purely diagnostic; does not affect control flow.
STALE_HEARTBEAT_THRESHOLD = timedelta(minutes=10)


def get_sequence_order(segment_code: str) -> int:
    """
    Resolve a segment's processing order from the fixed SEGMENT_ORDER list.

    Returns 1-7 for the recognized trade segments (index + 1), or 999 for
    any unrecognized code (sorts last rather than raising, so an unexpected
    segment_code can't crash the day's ordering).
    """
    try:
        return SEGMENT_ORDER.index(segment_code) + 1
    except ValueError:
        return 999


def get_segment_name(segment_code: str) -> str:
    """Resolve a segment's human display label from the fixed SEGMENT_NAMES map."""
    return SEGMENT_NAMES.get(segment_code, segment_code)
