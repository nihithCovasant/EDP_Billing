"""
Fixed constants for the EDP segment pipeline.

9 segments run sequentially through the generic 7-step pipeline (holiday
check -> get-or-reserve PID -> file upload poll -> trigger -> bill
posting/recon/contract note polls); none are special-cased. Then 5 T+1
post-trade processes run through a shorter 3-step pipeline (GTG poll ->
trigger -> confirm poll), stored as extra segment_execution rows for the
same trade_date, reusing the same status/lock/heartbeat machinery.
"""

from __future__ import annotations

from datetime import timedelta

# Fixed processing order — a code constant since the regulatory sequence
# doesn't change day to day; changing it requires a code change. MCXPHY/
# NCDEXPHY are the physical-settlement counterparts of MCX/NCDEX, run
# immediately after their respective segment.
SEGMENT_ORDER: tuple[str, ...] = (
    "EQ", "DR", "CUR", "SL", "MCX", "MCXPHY", "NCDEX", "NCDEXPHY", "MTF",
)

# Human display labels.
SEGMENT_NAMES: dict[str, str] = {
    "EQ": "Cash",
    "DR": "F&O",
    "CUR": "CD",
    "SL": "SLBM",
    "MCX": "MCX",
    "MCXPHY": "MCX Phy",
    "NCDEX": "NCDEX",
    "NCDEXPHY": "NCDEX Phy",
    "MTF": "MTF",
}

# Fixed processing order for the 5 T+1 post-trade processes — run once per
# trade_date, sequentially, AFTER (but not gated on) the 7 real segments.
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

# Default opening gate for all 5 post-trade processes ("T+1, 2:30am" per
# spec) — none of them start polling before this time on trade_date+1 unless
# a process has its own explicit window_start in workflow_json. There is no
# window_end deadline for any of them (they poll indefinitely until CBOS
# confirms).
POST_TRADE_FIRST_WINDOW_START = "02:30"

# Heartbeat staleness threshold — a segment is considered STALE (for display
# purposes only, not persisted) if it's IN_PROGRESS but hasn't had a
# heartbeat in this long. Purely diagnostic; does not affect control flow.
STALE_HEARTBEAT_THRESHOLD = timedelta(minutes=10)


def get_sequence_order(segment_code: str) -> int:
    """
    Resolve a code's processing order from the fixed SEGMENT_ORDER /
    POST_TRADE_ORDER lists.

    Returns 1-9 for the 9 real trade segments, 10-14 for the 5 post-trade
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
