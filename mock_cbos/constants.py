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
    "COLVAL",
    "COLALLOC",
    "MTFFT",
    "DMRPT",
    "DMSTMT",
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
# there's no CBOS integration for an arbitrary 6th process).
TRIGGER_ENDPOINT: dict[str, str] = {
    "COLVAL": "/v1/api/process/GetCollateralValuation",
    "COLALLOC": "/v1/api/process/MTFTradeProcessCollateralAllocation",
    "MTFFT": "/v1/api/process/MTFTradeProcessFundTransfer",
    "DMRPT": "/v1/api/process/DailyMarginReporting",
    "DMSTMT": "/v1/api/process/DailyMarginStatements",
}


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
                "trigger_endpoint": TRIGGER_ENDPOINT[code],
            }
            for code in POST_TRADE_ORDER
        ],
        "notes": (
            "GTG/confirm polls use file_process_status with "
            "Segment=<process_code> and ProcessName=<gtg_process_name from "
            "workflow config, or default_gtg_process_name above>. "
            "Triggers always hit the fixed trigger_endpoint for each "
            "process_code; only LOGINID is config-driven on those calls."
        ),
    }
