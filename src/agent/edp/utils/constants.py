"""
Fixed constants for the EDP segment pipeline.

Per the EDP Billing segment flow, the agent processes 7 segments sequentially
— CASH -> F&O -> CD -> SLBM -> MCX -> NCDEX -> MTF — each running through the
identical generic 7-step pipeline (holiday check -> get-or-reserve process ID
-> file upload poll -> single trigger -> bill posting / recon / contract note
polls). MTF is not special-cased — it is just the 7th segment in sequence,
driven through the exact same pipeline as every other segment.

Once all 7 segments finish for a trade_date, 5 T+1 post-trade processes run
(Collateral Valuation -> Collateral Allocation -> MTF Fund Transfer -> Daily
Margin Reporting -> Daily Margin Statements), each through a shorter, uniform
3-step pipeline (GTG poll -> trigger -> confirm poll). They are stored as
extra segment_execution rows for the same trade_date (see POST_TRADE_ORDER),
reusing all the same status/lock/heartbeat machinery as the 7 real segments.
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

# Fixed processing order for the 5 T+1 post-trade processes — run once per
# trade_date, sequentially, AFTER (but not gated on) the 7 real segments.
# Unlike the 7 segments, these are not part of the ops-uploaded workflow_json
# config: there is no per-day login_id/window to configure, so the order,
# names, and CBOS ProcessName mapping are all fixed code constants too.
POST_TRADE_ORDER: tuple[str, ...] = (
    "COLVAL", "COLALLOC", "MTFFT", "DMRPT", "DMSTMT",
)

POST_TRADE_NAMES: dict[str, str] = {
    "COLVAL": "Collateral Valuation",
    "COLALLOC": "Collateral Allocation",
    "MTFFT": "MTF Fund Transfer",
    "DMRPT": "Daily Margin Reporting",
    "DMSTMT": "Daily Margin Statements",
}

# CBOS file_process_status ProcessName used for the GTG poll (both before AND
# after the trigger call) of each post-trade process. "Segment" in that same
# call is always the pseudo segment_code itself (COLVAL, COLALLOC, ...).
POST_TRADE_GTG_PROCESS_NAME: dict[str, str] = {
    "COLVAL": "CollateralValuation",
    "COLALLOC": "CollateralAllocation",
    "MTFFT": "FundTransfer",
    "DMRPT": "DailyMarginReporting",
    "DMSTMT": "DailyMarginStatements",
}

# Only the FIRST post-trade process (Collateral Valuation) has an explicit
# opening gate per the spec ("T+1, 2:30am-6am window") — it won't start
# polling before this time on trade_date+1. The remaining 4 simply start as
# soon as the previous one in the chain completes; there is no window_end
# deadline for any of them (they poll indefinitely until CBOS confirms).
POST_TRADE_FIRST_WINDOW_START = "02:30"

# Heartbeat staleness threshold — a segment is considered STALE (for display
# purposes only, not persisted) if it's IN_PROGRESS but hasn't had a
# heartbeat in this long. Purely diagnostic; does not affect control flow.
STALE_HEARTBEAT_THRESHOLD = timedelta(minutes=10)


def get_sequence_order(segment_code: str) -> int:
    """
    Resolve a code's processing order from the fixed SEGMENT_ORDER /
    POST_TRADE_ORDER lists.

    Returns 1-7 for the 7 real trade segments, 8-12 for the 5 post-trade
    processes (so they always sort after every real segment on the day's
    status view), or 999 for any unrecognized code (sorts last rather than
    raising, so an unexpected segment_code can't crash the day's ordering).
    """
    try:
        return SEGMENT_ORDER.index(segment_code) + 1
    except ValueError:
        pass
    try:
        return len(SEGMENT_ORDER) + POST_TRADE_ORDER.index(segment_code) + 1
    except ValueError:
        return 999


def get_segment_name(segment_code: str) -> str:
    """Resolve a code's human display label from the fixed name maps."""
    if segment_code in SEGMENT_NAMES:
        return SEGMENT_NAMES[segment_code]
    return POST_TRADE_NAMES.get(segment_code, segment_code)


def is_post_trade_process(segment_code: str) -> bool:
    """True for the 5 T+1 post-trade pseudo-segment codes, False otherwise."""
    return segment_code in POST_TRADE_ORDER
