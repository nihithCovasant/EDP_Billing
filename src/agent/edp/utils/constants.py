"""
Fixed constants for the EDP segment pipeline.

9 segments run independently through the generic 6-state pipeline (INIT's
holiday check -> WAITING_FOR_FILE_UPLOAD's reserve-PID/upload poll ->
TRIGGERED -> WAITING_FOR_BILLPOSTING/_RECON/_CONTRACT_NOTE_GENERATION
polls); none are special-cased. Then 5 T+1 post-trade processes run through
a shorter 3-state pipeline (WAITING_FOR_GTG -> [TRIGGERED ->]
WAITING_FOR_COMPLETION), stored as extra segment_execution rows for the
same trade_date, reusing the same status/heartbeat machinery. See
models.SegmentState for the full state model.
"""

from __future__ import annotations

from datetime import timedelta

# Fixed processing order — a code constant since the regulatory sequence
# doesn't change day to day; changing it requires a code change. Matches
# EDP_Trade_Process_API_v3.docx's segment table exactly, minus MF (Mutual
# Fund), removed for now — see MfSegmentStateMachine git history to restore
# it. NCDEXPHY/MCXPHY are the physical-settlement counterparts of
# NCDEX/MCX, run immediately after their respective segment.
SEGMENT_ORDER: tuple[str, ...] = (
    "EQ", "DR", "CUR", "SLB", "NCDEX", "NCDEXPHY", "MCX", "MCXPHY", "NSECOM",
)

# Human display labels.
SEGMENT_NAMES: dict[str, str] = {
    "EQ": "Cash",
    "DR": "F&O",
    "CUR": "CD",
    "SLB": "SLB",
    "NCDEX": "NCDEX",
    "NCDEXPHY": "NCDEX Phy",
    "MCX": "MCX",
    "MCXPHY": "MCX Phy",
    "NSECOM": "NSE Commodity",
}

# Fixed processing order for the 5 T+1 post-trade processes — run once per
# trade_date, sequentially, AFTER (but not gated on) the 9 real segments.
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

# Segments whose entire window (both window_start AND window_end) falls on
# trade_date+1 rather than the trade_date-evening-into-next-morning rollover
# pattern most segments follow. A fixed regulatory characteristic (like
# SEGMENT_ORDER), not something workflow_json can override — plain HH:MM
# strings can't otherwise distinguish "04:00 next day" from "04:00 same day"
# the way an evening window_start + earlier window_end can (that case rolls
# window_end forward automatically once it's chronologically <= window_start
# on trade_date; see orchestrator._resolve_window()).
NEXT_DAY_WINDOW_SEGMENTS: frozenset[str] = frozenset({"MCX", "MCXPHY", "NSECOM"})

# Default opening gate for all 5 post-trade processes ("T+1, 2:30am" per
# spec) — none of them start polling before this time on trade_date+1 unless
# a process has its own explicit window_start in workflow_json.
POST_TRADE_FIRST_WINDOW_START = "02:30"

# Default closing deadline for all 5 post-trade processes ("T+1, 6:00am" per
# spec) — same trade_date+1 calendar day as the opening gate above, unless a
# process has its own explicit window_end in workflow_json. Without this, a
# post-trade process that CBOS never responds to would poll (BLOCKED)
# forever with no FAILED/TIMEOUT and no alert ever firing — the same
# window-deadline safety net the 9 real segments already get via
# orchestrator._resolve_window().
POST_TRADE_DEFAULT_WINDOW_END = "06:00"

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
