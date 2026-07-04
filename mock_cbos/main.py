"""
Mock CBOS Server
================

Standalone FastAPI app that simulates the CBOS endpoints used by the EDP
Billing segment execution flow (holiday check, get-or-reserve process ID,
file upload poll, single trigger, bill posting/recon/contract note polls —
identical for all 7 segments), so the EDP agent can be fully tested without
VPN/VDI access to the real MOFSL CBOS system.

Run standalone:
    python -m mock_cbos.main
    (or)
    uvicorn mock_cbos.main:app --host 0.0.0.0 --port 9100 --reload

This folder is completely self-contained and has ZERO imports from the
agent codebase (src/). To go live against the real CBOS system:
  1. Delete this folder — the agent is unaffected.
  2. Point CBOS_STATUS_URL / CBOS_PROCESS_URL (in .env) at the real CBOS
     base URLs instead of this mock server's URL, and set CBOS_USE_MOCK=false.

See README.md in this folder for full usage instructions.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .state import state, build_table2

app = FastAPI(
    title="Mock CBOS Server",
    description="Local simulator for MOFSL CBOS EDP Trade Process APIs (v2 doc)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Steps 1/3/5/6/7 — file_process_status (Good-to-Go polling)
#     EDP Status API — port 8087 in real CBOS
# =============================================================================

@app.post("/api/edp/file_process_status")
async def file_process_status(payload: dict):
    """
    Generic GTG / status poll used for every ProcessName in the flow —
    identical for all 7 segments (CASH/EQ, F&O/DR, CD/CUR, SLBM/SL, MCX,
    NCDEX, MTF):
      BeginFileUpload, FILEUPLOAD, BILLPOSTING, RECON, CONTRACTNOTEGENERATION.

    Body: {"Segment": "EQ", "ProcessName": "BeginFileUpload", "UserID": "CV0001"}
    """
    segment = str(payload.get("Segment", ""))
    process_name = str(payload.get("ProcessName", ""))

    msg = state.file_status(segment, process_name)
    return {"Status": "Success", "Data": [{"MSG": msg}]}


# =============================================================================
# Steps 2/4 — getNewTradeProcess (reserve PROCESSID="0" / execute with real PROCESSID)
#     Main Process API — port 8003 in real CBOS
# =============================================================================

@app.post("/v1/api/process/getNewTradeProcess")
async def get_new_trade_process(payload: dict):
    """
    Body: {"GROUPNAME":"EQ","LOGINID":"CV0001","TRADEDATE":"2026-06-29","PROCESSID":"0"}

    PROCESSID == "0" -> reserve a new process id (Step 2, reserve branch)
    PROCESSID == "<actual>" -> execute/trigger that process (Step 4)
    """
    group_name = str(payload.get("GROUPNAME", ""))
    trade_date = str(payload.get("TRADEDATE", ""))
    process_id = str(payload.get("PROCESSID", "0"))

    if process_id == "0":
        pid = state.reserve_process_id(group_name, trade_date)
        return {
            "Status": "Success",
            "Result": {
                "Table1": [{"PROCESSID": int(pid), "ISRUNNABLE": True, "ISAUTOUPLOAD": True}],
                "Table2": build_table2(all_success=False),
            },
        }

    state.mark_executed(process_id)
    return {
        "Status": "Success",
        "Result": {
            "Table1": [{"PROCESSID": int(process_id), "ISRUNNABLE": True}],
            "Table2": build_table2(all_success=True),
        },
    }


# =============================================================================
# Step 2 — getdropdown EXISTINGPROCESSID (get-or-reserve check)
#     Brokerage API — port 8003/v1/api/brokerage in real CBOS
# =============================================================================

@app.post("/v1/api/brokerage/getdropdown")
async def getdropdown(payload: dict):
    """
    Body: {"TAG":"EXISTINGPROCESSID","LOGINID":"CV0001","FILTER1":"EQ","FILTER2":"2026-06-29", ...}
    """
    tag = str(payload.get("TAG", ""))
    if tag != "EXISTINGPROCESSID":
        return {"Status": "Success", "Result": []}

    segment = str(payload.get("FILTER1", ""))
    trade_date = str(payload.get("FILTER2", ""))
    login_id = str(payload.get("LOGINID", ""))

    pid = state.find_reserved_pid(segment, trade_date)
    if not pid:
        return {"Status": "Success", "Result": []}

    desc = f"{pid} - {login_id} - {datetime.now().strftime('%b %d %Y %I:%M%p')}"
    return {"Status": "Success", "Result": [{"_KEY": int(pid), "_DESC": desc}]}


# =============================================================================
# File upload flow stubs (owned by RPA in the real pipeline — the EDP agent
#     never calls these; kept here only so the mock server matches the full
#     API doc for anyone testing the RPA/upload side separately).
# =============================================================================

@app.post("/v1/api/process/GetNewTradeProcessPromodalUploadSettings")
async def get_upload_settings(payload: dict):
    upload_id = payload.get("UPLOADID", "0")
    return {
        "Status": "Success",
        "Result": [{
            "ID": int(upload_id),
            "NAME": f"MOCK UPLOAD {upload_id}",
            "FILE NAME (CONTAINS)": "MOCK",
            "FILEEXTENSION": "TXT",
            "NO. OF COLUMNS": 10,
        }],
    }


@app.post("/v1/api/process/SaveTradePromodalUploadChunkFile")
async def save_chunk_file(request: Request):
    form = await request.form()
    return {
        "Status": "ChunkUploaded",
        "Guid": str(uuid.uuid4()),
        "FileName": form.get("FileName", "MOCK_FILE.TXT"),
        "currentChunk": form.get("CurrentChunk", "0"),
        "totalChunks": form.get("TotalChunks", "1"),
        "fCount": "1",
    }


@app.post("/v1/api/process/SaveNewTradeProcessPromodalUploadFile")
async def save_upload_file(payload: dict):
    return {"Status": "Success", "Result": "File entry created successfully."}


# =============================================================================
# Admin / control endpoints — for QA to script test scenarios
# =============================================================================

@app.get("/mock/health")
async def health():
    return {"status": "ok", "server": "mock-cbos"}


@app.get("/mock/state")
async def get_state():
    """Dump all in-memory poll counters / reserved PIDs for debugging."""
    return state.snapshot()


@app.post("/mock/reset")
async def reset_state():
    """Clear all poll counters, reserved PIDs, and scenario overrides."""
    state.reset()
    return {"status": "reset"}


@app.post("/mock/config/ready_after")
async def set_ready_after(payload: dict):
    """
    Set how many polls a GTG check needs before returning TRUE.
    Body: {"ready_after": 3}
    """
    n = int(payload.get("ready_after", 2))
    state.set_ready_after(n)
    return {"ready_after": state.ready_after}


@app.post("/mock/scenario/holiday")
async def set_holiday(payload: dict):
    """
    Force BeginFileUpload to return SKIP for a segment (holiday simulation).
    Body: {"segment": "EQ", "enabled": true}
    """
    state.set_holiday(payload.get("segment", ""), bool(payload.get("enabled", True)))
    return {"holiday_segments": sorted(state.holiday_segments)}


@app.post("/mock/scenario/stuck")
async def set_stuck(payload: dict):
    """
    Pin a (segment, process_name) pair to always return FALSE —
    useful for testing window-deadline TIMEOUT handling.
    Body: {"segment": "EQ", "process_name": "FILEUPLOAD", "enabled": true}
    """
    state.set_stuck(
        payload.get("segment", ""),
        payload.get("process_name", ""),
        bool(payload.get("enabled", True)),
    )
    return {"stuck_keys": sorted(f"{k[0]}::{k[1]}" for k in state.stuck_keys)}


@app.post("/mock/scenario/force_ready")
async def set_force_ready(payload: dict):
    """
    Pin a (segment, process_name) pair to always return TRUE immediately —
    useful for skipping past slow polling loops during manual testing.
    Body: {"segment": "EQ", "process_name": "BILLPOSTING", "enabled": true}
    """
    state.set_force_ready(
        payload.get("segment", ""),
        payload.get("process_name", ""),
        bool(payload.get("enabled", True)),
    )
    return {"force_ready_keys": sorted(f"{k[0]}::{k[1]}" for k in state.force_ready_keys)}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("MOCK_CBOS_PORT", "9100"))
    host = os.getenv("MOCK_CBOS_HOST", "0.0.0.0")
    uvicorn.run("mock_cbos.main:app", host=host, port=port, reload=True)
