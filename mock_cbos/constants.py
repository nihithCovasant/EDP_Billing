"""
Standalone reference constants for the 5 T+1 post-trade processes.

Mirrors the agent's default mapping (src/agent/edp/utils/constants.py) but
this module intentionally imports NOTHING from src/ — mock_cbos must remain
fully self-contained.

The EDP agent now resolves login_id, gtg_process_name, and window_start from
the ops-uploaded workflow_json["post_trade_processes"] list at runtime.
These defaults are what the agent falls back to when a config entry omits
gtg_process_name — and what the /mock/scenario/post_trade_* convenience
endpoints use when you pass only process_code (no explicit gtg_process_name).
"""

from __future__ import annotations

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

# Default CBOS file_process_status ProcessName per process_code — overridden
# per upload when workflow_json.post_trade_processes[].gtg_process_name is set.
DEFAULT_GTG_PROCESS_NAME: dict[str, str] = {
    "COLVAL": "CollateralValuation",
    "COLALLOC": "CollateralAllocation",
    "MTFFT": "FundTransfer",
    "DMRPT": "DailyMarginReporting",
    "DMSTMT": "DailyMarginStatements",
}

# Fixed CBOS trigger endpoint path per process_code (not config-driven —
# there's no CBOS integration for an arbitrary 6th process). DMRPT shares
# CombinedMarginProcess with its own already-triggered/REFRESH check
# (BUTTONNAME-dispatched, same as GetCollateralValuation). DMSTMT has no
# PROCESS-API trigger endpoint at all — it fires through the STATUS API
# (file_process_status ProcessName=DAILYMARGINSTATEMENT, see
# ALREADY_TRIGGERED_PROCESS_NAME below), so it's intentionally absent here.
TRIGGER_ENDPOINT: dict[str, str] = {
    "COLVAL": "/v1/api/process/GetCollateralValuation",
    "COLALLOC": "/v1/api/process/MTFTradeProcessCollateralAllocation",
    "MTFFT": "/v1/api/process/MTFTradeProcessFundTransfer",
    "DMRPT": "/v1/api/process/CombinedMarginProcess",
}

# ProcessName values used ONLY by the 3 "already triggered" pre-checks that
# share file_process_status instead of a REFRESH-style PROCESS-API call
# (COLALLOC/MTFFT/DMSTMT — see src/tools/cbos_client.py
# _already_triggered_via_file_status()). These must reflect actual trigger
# state (state.post_trade_triggered), not the generic poll-count GTG logic,
# or the check answer is decoupled from reality (agent could skip a trigger
# that never fired, or refuse one that already did).
ALREADY_TRIGGERED_PROCESS_NAME: dict[str, str] = {
    "MTFCOLLALLOC": "COLALLOC",
    "MTFFUNDTRAN": "MTFFT",
    "CHECKDAILYMARGINSTATEMENT": "DMSTMT",
}

# The one-shot DMSTMT trigger ProcessName (STATUS API, ALL CAPS, distinct
# from POST_TRADE_GTG_PROCESS_NAME's "DailyMarginStatements" used for the
# WAITING_FOR_COMPLETION poll) — confirmed against EDP_Trade_Process_API_v3
# Step 38. Real CBOS fires-and-acks immediately here; it is NOT a
# poll-until-ready gate like the other ProcessNames, so it must not share
# the generic poll-count logic either.
DMSTMT_TRIGGER_PROCESS_NAME = "DAILYMARGINSTATEMENT"


def resolve_gtg_process_name(process_code: str, override: str | None = None) -> str:
    """Config override wins; else the fixed default; else the raw process_code."""
    if override:
        return override
    return DEFAULT_GTG_PROCESS_NAME.get(process_code.upper(), process_code)


def post_trade_reference() -> dict:
    """Shape returned by GET /mock/reference/post_trade for QA / scripting."""
    return {
        "process_order": list(POST_TRADE_ORDER),
        "processes": [
            {
                "process_code": code,
                "display_name": POST_TRADE_NAMES[code],
                "default_gtg_process_name": DEFAULT_GTG_PROCESS_NAME[code],
                "trigger_endpoint": TRIGGER_ENDPOINT.get(
                    code, f"file_process_status(ProcessName={DMSTMT_TRIGGER_PROCESS_NAME})"
                ),
            }
            for code in POST_TRADE_ORDER
        ],
        "notes": (
            "GTG/confirm polls use file_process_status with "
            "Segment=<process_code> and ProcessName=<gtg_process_name from "
            "workflow config, or default_gtg_process_name above>. "
            "Triggers hit the fixed trigger_endpoint for each process_code "
            "(BUTTONNAME-dispatched for COLVAL/DMRPT — see "
            "ALREADY_TRIGGERED_PROCESS_NAME for their REFRESH-variant "
            "already-triggered check), except DMSTMT which has no PROCESS-API "
            "trigger endpoint and instead fires via file_process_status "
            f"(ProcessName={DMSTMT_TRIGGER_PROCESS_NAME})."
        ),
    }
