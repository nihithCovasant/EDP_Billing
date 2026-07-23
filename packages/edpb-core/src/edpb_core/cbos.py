"""CBOS v6 contract constants and payload builders
(EDP_Trade_Process_API_Documentation_V6.docx; OpenAPI mirror in
EDPBilling_FIle_Upload/docs/postman/edp_trade_process_openapi.json).

Services keep their own HTTP clients (sync in the uploader, async in the
engine) — what they share is the WIRE SHAPE, declared once here. V5's headline
change (retained in V6): file_process_status carries TradeDate immediately
after Segment ("Shape A"); getNewTradeProcess with a real PROCESSID re-fetches
instead of minting; getdropdown(EXISTINGPROCESSID) filters by segment + trade
date. V6's headline change: a new Step 10 — Insti Trade Status GTG
(ProcessName=CHECKINSTITRADE, Shape A) — must answer TRUE after FILEUPLOAD
does and before the trade-process trigger fires; old doc Steps 10-39
renumber to 11-40, with no other payload change.
"""

from __future__ import annotations

# ---- endpoint paths -------------------------------------------------------
# CORE host (process/brokerage APIs)
GET_NEW_TRADE_PROCESS_PATH = "/v1/api/process/getNewTradeProcess"
UPLOAD_SETTINGS_PATH = "/v1/api/process/GetNewTradeProcessPromodalUploadSettings"
UPLOAD_CHUNK_PATH = "/v1/api/process/SaveTradePromodalUploadChunkFile"
REGISTER_FILE_PATH = "/v1/api/process/SaveNewTradeProcessPromodalUploadFile"
UPDATE_IS_MANDATORY_PATH = "/v1/api/process/UpdateNewTradeProcessProcessDetailsIsMandatory"
GET_DROPDOWN_PATH = "/v1/api/brokerage/getdropdown"
# GTG host (good-to-go / status)
FILE_PROCESS_STATUS_PATH = "/api/edp/file_process_status"
GET_EXPECTED_FILENAME_PATH = "/api/edp/get_expected_filename"

# ---- well-known process names + messages ----------------------------------
PROCESS_BEGIN_FILE_UPLOAD = "BeginFileUpload"
PROCESS_CHECK_PROCESS_ID = "CheckProcessIDExist"
PROCESS_FILE_UPLOAD_STATUS = "FILEUPLOAD"
# V6 Step 10: Insti Trade Status GTG — TRUE = Institutional Trade Transfer
# complete, safe to trigger; FALSE = still in progress. CBOS does NOT
# enforce this gate server-side (the doc warns early triggers "may cause
# pipeline step failures"), so the engine polls it between FILEUPLOAD and
# the trigger call.
PROCESS_CHECK_INSTI_TRADE = "CHECKINSTITRADE"
BEGIN_UPLOAD_PROCEED = "SKIP"  # counter-intuitive: SKIP means "not a holiday, proceed"
MSG_TRUE = "TRUE"
MSG_FALSE = "FALSE"


def file_process_status_payload(
    segment: str,
    trade_date_iso: str,
    process_name: str,
    user_id: str,
) -> dict[str, str]:
    """V5 "Shape A" (retained in V6): TradeDate is required, immediately
    after Segment. Every segment-scoped file_process_status call (Steps
    1/3/9, V6's new Step-10 CHECKINSTITRADE gate, and the downstream
    BILLPOSTING / RECON / CONTRACTNOTEGENERATION polls) uses this shape."""
    return {
        "Segment": segment,
        "TradeDate": trade_date_iso,
        "ProcessName": process_name,
        "UserID": user_id,
    }


def get_new_trade_process_payload(
    segment: str,
    trade_date_iso: str,
    login_id: str,
    password: str,
    process_id: str = "0",
) -> dict[str, str]:
    """PROCESSID='0' mints (UPLOADER ONLY — single-writer contract, see
    CBOS_HANDOFF_CONTRACT.md); a real PROCESSID re-fetches (uploader) or
    triggers (engine, once FILEUPLOAD is TRUE)."""
    return {
        "GROUPNAME": segment,
        "LOGINID": login_id,
        "PASSWORD": password,
        "TRADEDATE": trade_date_iso,
        "PROCESSID": process_id,
    }


def existing_process_id_payload(
    segment: str,
    trade_date_iso: str,
    login_id: str,
) -> dict[str, str]:
    """getdropdown(EXISTINGPROCESSID): FILTER1 = segment, FILTER2 = trade date
    (V5 — the date filter is what stops yesterday's PID leaking into today)."""
    return {
        "TAG": "EXISTINGPROCESSID",
        "LOGINID": login_id,
        "FILTER1": segment,
        "FILTER2": trade_date_iso,
        "extraoption2": "",
        "extraoption3": "",
    }


def file_process_status_payload_b(
    trade_date_iso: str,
    process_name: str,
    user_id: str,
) -> dict[str, str]:
    """V5 "Shape B" (retained in V6) — the file_process_status steps WITHOUT
    a Segment field (post-trade GTG/already-triggered/completion checks:
    V6 doc steps 17, 20-21, 23-24, 29, 31-32, 38-39, per the per-step
    payload examples — the doc's own Shape A/B summary lists are
    self-contradictory, and its steps 14/16 examples DO carry Segment
    despite being listed as Shape B; the per-step examples are the
    authority). TradeDate leads."""
    return {
        "TradeDate": trade_date_iso,
        "ProcessName": process_name,
        "UserID": user_id,
    }
