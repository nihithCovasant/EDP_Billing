"""
Full 26-step reference table from EDP_Trade_Process_API_v2.docx ("Quick
Reference — All API Endpoints").

The real CBOS API only exposes 10 distinct URLs; the 26 "steps" are usage
scenarios of those 10 URLs with different Segment / ProcessName / TYPE
values. This module exists purely so the mock server can prove — via
GET /mock/steps — that every one of the 26 documented steps is served by
this mock, even though only 10 routes appear in the FastAPI /docs page.
"""

from __future__ import annotations

from typing import List, TypedDict


class ApiStep(TypedDict):
    step: int
    purpose: str
    method: str
    path: str
    mock_status: str
    example_discriminator: str


ALL_STEPS: List[ApiStep] = [
    {"step": 1, "purpose": "Holiday Check", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "ProcessName=BeginFileUpload"},
    {"step": 2, "purpose": "Create Process ID", "method": "POST",
     "path": "/v1/api/process/getNewTradeProcess", "mock_status": "implemented",
     "example_discriminator": "PROCESSID=0"},
    {"step": 3, "purpose": "Get Upload Settings", "method": "POST",
     "path": "/v1/api/process/GetNewTradeProcessPromodalUploadSettings",
     "mock_status": "stub (RPA-owned, agent never calls this)",
     "example_discriminator": "UPLOADID=<id>"},
    {"step": 4, "purpose": "Chunk File Upload", "method": "POST",
     "path": "/v1/api/process/SaveTradePromodalUploadChunkFile",
     "mock_status": "stub (RPA-owned, agent never calls this)",
     "example_discriminator": "multipart/form-data"},
    {"step": 5, "purpose": "Get Existing Process ID", "method": "POST",
     "path": "/v1/api/brokerage/getdropdown", "mock_status": "implemented",
     "example_discriminator": "TAG=EXISTINGPROCESSID"},
    {"step": 6, "purpose": "File Entry in CBOS", "method": "POST",
     "path": "/v1/api/process/SaveNewTradeProcessPromodalUploadFile",
     "mock_status": "stub (RPA-owned, agent never calls this)",
     "example_discriminator": "uploadid, uploadfoldername"},
    {"step": 7, "purpose": "File Upload Status Check", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "ProcessName=FILEUPLOAD"},
    {"step": 8, "purpose": "Execute Trade Process", "method": "POST",
     "path": "/v1/api/process/getNewTradeProcess", "mock_status": "implemented",
     "example_discriminator": "PROCESSID=<actual>"},
    {"step": 9, "purpose": "Bill Posting Status (EQ)", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=EQ, ProcessName=BILLPOSTING"},
    {"step": 10, "purpose": "Segment Recon (DR)", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=DR, ProcessName=RECON"},
    {"step": 11, "purpose": "Contract Note Generation (DR)", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=DR, ProcessName=CONTRACTNOTEGENERATION"},
    {"step": 12, "purpose": "Collateral Valuation GTG", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=DR, ProcessName=CollateralValuation"},
    {"step": 13, "purpose": "Collateral Valuation Trigger", "method": "POST",
     "path": "/v1/api/process/GetCollateralValuation", "mock_status": "implemented",
     "example_discriminator": "LOGINID=G_LID, MARGINDATE"},
    {"step": 14, "purpose": "Collateral Allocation GTG", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=DR, ProcessName=CollateralAllocation"},
    {"step": 15, "purpose": "Collateral Allocation Trigger", "method": "POST",
     "path": "/v1/api/process/MTFTradeProcessCollateralAllocation", "mock_status": "implemented",
     "example_discriminator": "LOGINID=G_LID, TRADEDATE"},
    {"step": 16, "purpose": "Fund Transfer GTG", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=DR, ProcessName=FundTransfer"},
    {"step": 17, "purpose": "Fund Transfer Trigger", "method": "POST",
     "path": "/v1/api/process/MTFTradeProcessFundTransfer", "mock_status": "implemented",
     "example_discriminator": "LOGINID=G_LID, TRADEDATE (see /mock/scenario/fund_transfer_quirk)"},
    {"step": 18, "purpose": "Cash Bill Posting GTG (MTF)", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=EQ, ProcessName=BILLPOSTING"},
    {"step": 19, "purpose": "MTF Buy Trade Process", "method": "POST",
     "path": "/v1/api/process/MTFTradeProcess", "mock_status": "implemented",
     "example_discriminator": "TYPE=BUY PROCESS"},
    {"step": 20, "purpose": "MTF Buy Trade Posting", "method": "POST",
     "path": "/v1/api/process/MTFTradeProcess", "mock_status": "implemented",
     "example_discriminator": "TYPE=BUY POSTING"},
    {"step": 21, "purpose": "MTF Sell GTG", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=EQ, ProcessName=EARLYPAYIN"},
    {"step": 22, "purpose": "MTF Sell Process & Posting", "method": "POST",
     "path": "/v1/api/process/MTFTradeProcess", "mock_status": "implemented",
     "example_discriminator": "TYPE=SELL PROCESS AND POSTING"},
    {"step": 23, "purpose": "Weekly Auto Closure GTG", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=EQ, ProcessName=WEEKLYAUTOCLOSURE"},
    {"step": 24, "purpose": "Weekly Auto Closure", "method": "POST",
     "path": "/v1/api/process/MTFTradeProcess", "mock_status": "implemented",
     "example_discriminator": "TYPE=WEEKLY AUTOCLOSURE"},
    {"step": 25, "purpose": "Corp Action FO Bill GTG (DR)", "method": "POST",
     "path": "/api/edp/file_process_status", "mock_status": "implemented",
     "example_discriminator": "Segment=DR, ProcessName=BILLPOSTING"},
    {"step": 26, "purpose": "Corporate Action Position Change", "method": "POST",
     "path": "/v1/api/process/getNewTradeProcess",
     "mock_status": "out of scope (requires manual Ops file drops — by design decision)",
     "example_discriminator": "GROUPNAME=FOPositionChange"},
]
