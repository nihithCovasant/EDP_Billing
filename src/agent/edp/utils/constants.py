"""
Fixed constants for the post-segment MTF operations chain.

Per EDP_Trade_Process_API_v2.docx (steps 12-24), after all trade segments
(EQ, DR, CUR, SLB, NCDEX, MCX, NSECOM, MF) complete, MOFSL runs a further
6-step chain: Collateral Valuation -> Collateral Allocation -> Fund Transfer
-> MTF Buy -> MTF Sell -> Weekly Auto Closure.

These steps are NOT per-segment — they run once per trading day, gated on
fixed segments (DR / EQ) regardless of which segment code triggered them.
We model this as one virtual "segment" so it reuses the existing sequencing,
locking, and window-deadline machinery in segment_execution without any
schema change.

Step 26 (Corporate Action Position Change) is intentionally NOT implemented —
it depends on manual Ops file drops between 10PM-11:59PM and was explicitly
scoped out.
"""

from __future__ import annotations

from datetime import timedelta

# Fixed processing order for the 8 real trade segments. This used to be a
# per-day config field (workflow_json.segments[].sequence_order) mirrored
# onto a segment_execution DB column; it is now a code constant since the
# regulatory sequence (EQ -> DR -> CUR -> SL -> NCDEX -> MCX -> NSECOM -> MF)
# does not change day to day. Changing the order requires a code change.
SEGMENT_ORDER: tuple[str, ...] = (
    "EQ", "DR", "CUR", "SL", "NCDEX", "MCX", "NSECOM", "MF",
)

# Human display labels — also used to be a per-day config field
# (workflow_json.segments[].segment_name) mirrored onto a segment_execution
# DB column; folded into a code constant for the same reason as SEGMENT_ORDER.
SEGMENT_NAMES: dict[str, str] = {
    "EQ": "Cash",
    "DR": "Derivative",
    "CUR": "Currency",
    "SL": "SLB",
    "NCDEX": "NCDEX Commodity",
    "MCX": "MCX Commodity",
    "NSECOM": "NSE Commodity",
    "MF": "Mutual Fund",
}

# Virtual segment representing the post-segment MTF operations chain.
# Given the highest sequence order so it only starts once every real
# trade segment has reached COMPLETED or SKIPPED.
MTF_OPS_SEGMENT_CODE = "MTFOPS"
MTF_OPS_SEGMENT_NAME = "MTF Operations (Collateral / Fund Transfer / Buy-Sell)"
MTF_OPS_SEQUENCE_ORDER = 900

# Heartbeat staleness threshold — a segment is considered STALE (for display
# purposes only, not persisted) if it's IN_PROGRESS but hasn't had a
# heartbeat in this long. Purely diagnostic; does not affect control flow.
STALE_HEARTBEAT_THRESHOLD = timedelta(minutes=10)


def get_sequence_order(segment_code: str) -> int:
    """
    Resolve a segment's processing order from the fixed SEGMENT_ORDER list.

    Returns 1-8 for the real trade segments (index + 1), MTF_OPS_SEQUENCE_ORDER
    for the virtual MTFOPS segment, or 999 for any unrecognized code (sorts
    last rather than raising, so an unexpected segment_code can't crash the
    day's ordering).
    """
    if segment_code == MTF_OPS_SEGMENT_CODE:
        return MTF_OPS_SEQUENCE_ORDER
    try:
        return SEGMENT_ORDER.index(segment_code) + 1
    except ValueError:
        return 999


def get_segment_name(segment_code: str) -> str:
    """Resolve a segment's human display label from the fixed SEGMENT_NAMES map."""
    if segment_code == MTF_OPS_SEGMENT_CODE:
        return MTF_OPS_SEGMENT_NAME
    return SEGMENT_NAMES.get(segment_code, segment_code)


# Trigger calls (Steps 13, 15, 17, 19, 20, 22, 24) always use this fixed
# LOGINID per the API doc — distinct from the per-segment CV0001 login used
# for GTG checks and the main 7-stage pipeline.
MTF_TRIGGER_LOGIN_ID = "G_LID"

# GTG (file_process_status) checks in the MTF chain are hardcoded to a
# specific segment in the API doc, not the (virtual) segment being processed.
COLLATERAL_GTG_SEGMENT = "DR"   # Steps 12, 14, 16
MTF_GTG_SEGMENT = "EQ"          # Steps 18, 21, 23
