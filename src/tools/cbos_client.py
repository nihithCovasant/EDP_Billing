"""
CBOS HTTP client — exact MOFSL API contract.

Two separate base URLs:
  STATUS_URL  (port 8087) — Good-to-Go / completion checks via file_process_status
  PROCESS_URL (port 8003) — process management (reserve PID, trigger, crash-recovery)

Pipeline per segment (7 stages):
  1. file_process_status(BeginFileUpload)       → holiday check
  2. get_new_trade_process(PROCESSID="0")        → reserve process_id from CBOS
  3. file_process_status(FILEUPLOAD)             → poll until exchange files uploaded
  4. get_new_trade_process(PROCESSID=<actual>)   → trigger billing processing
  5. file_process_status(BILLPOSTING)            → poll until bill posting done
  6. file_process_status(RECON)                  → poll until reconciliation done
  7. file_process_status(CONTRACTNOTEGENERATION) → poll until contract notes done

ProcessName values for file_process_status:
  BeginFileUpload        — holiday gate (SKIP = holiday, TRUE = go, FALSE = not open yet)
  FILEUPLOAD             — all exchange files received gate
  BILLPOSTING            — billing calculations complete
  RECON                  — reconciliation complete
  CONTRACTNOTEGENERATION — contract note generation complete

CBOS API request / response shapes
  file_process_status
    POST {STATUS_URL}/api/edp/file_process_status
    Body: {"Segment":"EQ","ProcessName":"BeginFileUpload","UserID":"CV0001"}
    OK:   {"Status":"Success","Data":[{"MSG":"TRUE|FALSE|SKIP"}]}

  getNewTradeProcess  (reserve → PROCESSID="0", trigger → PROCESSID=actual)
    POST {PROCESS_URL}/v1/api/process/getNewTradeProcess
    Body: {"GROUPNAME":"EQ","LOGINID":"CV0001","TRADEDATE":"2026-06-29","PROCESSID":"0"}
    OK:   {"Status":"Success","Result":{"Table1":[{"PROCESSID":17658,"ISRUNNABLE":true,...}],"Table2":[...]}}

  getdropdown EXISTINGPROCESSID  (crash recovery)
    POST {PROCESS_URL}/v1/api/brokerage/getdropdown
    Body: {"TAG":"EXISTINGPROCESSID","LOGINID":"CV0001","FILTER1":"EQ","FILTER2":"2026-06-29","extraoption2":"","extraoption3":""}
    OK:   {"Status":"Success","Result":[{"_KEY":17658,"_DESC":"17658 - CV0001 - Jun 29 2026 2:19PM"}]}

Post-segment MTF operations chain (v2 doc steps 12-24) — runs once per day on
the virtual MTFOPS segment after ALL real segments complete. All 4 triggers
below use LOGINID="G_LID" (hardcoded) and DD-MMM-YYYY dates:
  GetCollateralValuation                  (step 13) — MARGINDATE
  MTFTradeProcessCollateralAllocation     (step 15) — TRADEDATE
  MTFTradeProcessFundTransfer             (step 17) — TRADEDATE
  MTFTradeProcess                         (steps 19/20/22/24) — TRADEDATE + TYPE
    TYPE: "BUY PROCESS" | "BUY POSTING" | "SELL PROCESS AND POSTING" | "WEEKLY AUTOCLOSURE"

  Their GTG checks reuse file_process_status with ProcessName:
    CollateralValuation (Segment=DR), CollateralAllocation (Segment=DR),
    FundTransfer (Segment=DR), BILLPOSTING (Segment=EQ), EARLYPAYIN (Segment=EQ),
    WEEKLYAUTOCLOSURE (Segment=EQ)

NOTE: Step 26 (Corporate Action Position Change) is intentionally NOT
implemented — it requires manual Ops file drops and was scoped out.
"""

from __future__ import annotations

import hashlib
import json as _json
import time as _time
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

import httpx

from cams_otel_lib import Logger as logger, otel_trace


# =============================================================================
# Result dataclasses
# =============================================================================

@dataclass
class FileStatusResult:
    """Result of a file_process_status call."""
    response: str              # "TRUE" | "FALSE" | "SKIP"
    raw_body: str = ""
    http_status: int = 200
    error: Optional[str] = None
    is_transient: bool = False  # True for network errors and HTTP 5xx (retryable)

    @property
    def is_ready(self) -> bool:
        return self.response.upper() == "TRUE"

    @property
    def is_skip(self) -> bool:
        return self.response.upper() == "SKIP"

    @property
    def is_pending(self) -> bool:
        return self.response.upper() == "FALSE"

    @property
    def is_error(self) -> bool:
        return self.error is not None


@dataclass
class NewTradeProcessStep:
    """One processing step from Table2 of the getNewTradeProcess response."""
    id: int
    step_no: int
    name: str
    status: str                # "PENDING" | "SUCCESS" | "FAILED"
    status_desc: Optional[str]
    upload_id: int
    start_datetime: Optional[str]
    end_datetime: Optional[str]


@dataclass
class NewTradeProcessResult:
    """Result of a getNewTradeProcess call (both reserve-PID and trigger modes)."""
    success: bool
    process_id: Optional[str] = None
    is_runnable: bool = False
    is_auto_upload: bool = False
    steps: List[NewTradeProcessStep] = field(default_factory=list)
    raw_body: str = ""
    http_status: int = 200
    error: Optional[str] = None
    is_transient: bool = False  # True for network errors and HTTP 5xx (retryable)


@dataclass
class ExistingProcessResult:
    """Result of a getdropdown EXISTINGPROCESSID call — used for crash recovery."""
    found: bool
    process_id: Optional[str] = None
    description: Optional[str] = None
    raw_body: str = ""
    error: Optional[str] = None
    is_transient: bool = False


@dataclass
class MtfTriggerResult:
    """
    Result of a fire-and-forget MTF trigger call — used for:
      GetCollateralValuation, MTFTradeProcessCollateralAllocation,
      MTFTradeProcessFundTransfer, MTFTradeProcess (BUY/SELL/WEEKLY AUTOCLOSURE)

    These endpoints return a plain success message, not structured data.
    """
    success: bool
    message: str = ""
    raw_body: str = ""
    http_status: int = 200
    error: Optional[str] = None
    is_transient: bool = False


def to_ddmmmyyyy(d: date) -> str:
    """
    Format a date as DD-MMM-YYYY (e.g. '19-Jun-2026'), as required by the
    MTF trigger endpoints (GetCollateralValuation, MTFTradeProcess*).
    Distinct from the YYYY-MM-DD format used by getNewTradeProcess.
    """
    return d.strftime("%d-%b-%Y")


# =============================================================================
# Client
# =============================================================================

class CbosClient:
    """
    Async HTTP client for all CBOS API calls.

    use_mock=True  → returns deterministic local responses, no network required.
    use_mock=False → hits the real CBOS endpoints (requires VPN / corporate network).
    """

    def __init__(
        self,
        status_url: str,
        process_url: str,
        use_mock: bool = True,
        timeout_seconds: float = 30.0,
    ):
        self.status_url = status_url.rstrip("/")    # http://10.167.202.234:8087
        self.process_url = process_url.rstrip("/")  # http://10.167.202.164:8003
        self.use_mock = use_mock
        self.timeout = timeout_seconds

        # Mock state — controls when polls "become ready"
        self._mock_ready_after: int = 2
        self._mock_call_counts: dict[str, int] = {}

    # -------------------------------------------------------------------------
    # 1. file_process_status — Good-to-Go / completion checks
    # -------------------------------------------------------------------------

    @otel_trace
    async def file_process_status(
        self,
        segment: str,
        process_name: str,
        user_id: str,
    ) -> FileStatusResult:
        """
        POST {STATUS_URL}/api/edp/file_process_status
        Returns TRUE (ready), FALSE (not yet), or SKIP (holiday/not applicable).
        """
        if self.use_mock:
            result = self._mock_file_status(segment, process_name)
            logger.info(
                f"[CBOS][MOCK] segment={segment} api=file_process_status "
                f"process={process_name} | response={result.response}"
            )
            return result

        url = f"{self.status_url}/api/edp/file_process_status"
        payload = {"Segment": segment, "ProcessName": process_name, "UserID": user_id}
        logger.info(
            f"[CBOS] segment={segment} api=file_process_status process={process_name} "
            f"| POST {url}"
        )

        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                body = resp.text[:2000]
                if resp.status_code != 200:
                    logger.error(
                        f"[CBOS] segment={segment} api=file_process_status process={process_name} "
                        f"| HTTP {resp.status_code} elapsed_ms={elapsed_ms}"
                    )
                    return FileStatusResult(
                        response="FALSE",
                        raw_body=body,
                        http_status=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                        is_transient=resp.status_code >= 500,
                    )
                msg = _parse_msg(body)
                logger.info(
                    f"[CBOS] segment={segment} api=file_process_status process={process_name} "
                    f"| response={msg} elapsed_ms={elapsed_ms}"
                )
                return FileStatusResult(response=msg, raw_body=body, http_status=200)
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            logger.error(
                f"[CBOS] segment={segment} api=file_process_status process={process_name} "
                f"| EXCEPTION elapsed_ms={elapsed_ms} error={exc}"
            )
            return FileStatusResult(response="FALSE", error=str(exc), is_transient=True)

    # -------------------------------------------------------------------------
    # 2 & 4. get_new_trade_process — reserve PID then trigger
    # -------------------------------------------------------------------------

    @otel_trace
    async def get_new_trade_process(
        self,
        group_name: str,
        login_id: str,
        trade_date: date,
        process_id: str = "0",
    ) -> NewTradeProcessResult:
        """
        POST {PROCESS_URL}/v1/api/process/getNewTradeProcess

        Called twice per segment-day:
          1. process_id="0"       → CBOS allocates and returns a new PROCESSID
          2. process_id="<actual>" → CBOS starts running all billing/calc steps
        """
        if self.use_mock:
            result = self._mock_new_trade_process(group_name, process_id)
            mode = "reserve_pid" if process_id == "0" else f"trigger(pid={process_id})"
            logger.info(
                f"[CBOS][MOCK] segment={group_name} api=getNewTradeProcess mode={mode} "
                f"| pid={result.process_id} success={result.success}"
            )
            return result

        url = f"{self.process_url}/v1/api/process/getNewTradeProcess"
        payload = {
            "GROUPNAME": group_name,
            "LOGINID": login_id,
            "TRADEDATE": trade_date.isoformat(),
            "PROCESSID": str(process_id),
        }
        mode = "reserve_pid" if process_id == "0" else f"trigger(pid={process_id})"
        logger.info(
            f"[CBOS] segment={group_name} api=getNewTradeProcess mode={mode} "
            f"| POST {url}"
        )

        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                body = resp.text[:5000]
                if resp.status_code != 200:
                    logger.error(
                        f"[CBOS] segment={group_name} api=getNewTradeProcess mode={mode} "
                        f"| HTTP {resp.status_code} elapsed_ms={elapsed_ms}"
                    )
                    return NewTradeProcessResult(
                        success=False,
                        raw_body=body,
                        http_status=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                        is_transient=resp.status_code >= 500,
                    )
                result = _parse_new_trade_process(body)
                logger.info(
                    f"[CBOS] segment={group_name} api=getNewTradeProcess mode={mode} "
                    f"| pid={result.process_id} success={result.success} "
                    f"steps={len(result.steps)} elapsed_ms={elapsed_ms}"
                )
                return result
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            logger.error(
                f"[CBOS] segment={group_name} api=getNewTradeProcess mode={mode} "
                f"| EXCEPTION elapsed_ms={elapsed_ms} error={exc}"
            )
            return NewTradeProcessResult(success=False, error=str(exc), is_transient=True)

    # -------------------------------------------------------------------------
    # 5. get_existing_process_id — crash recovery lookup
    # -------------------------------------------------------------------------

    @otel_trace
    async def get_existing_process_id(
        self,
        segment: str,
        login_id: str,
        trade_date: date,
    ) -> ExistingProcessResult:
        """
        POST {PROCESS_URL}/v1/api/brokerage/getdropdown
        Retrieves the most recently reserved PROCESSID for a segment+date.
        Used to recover after an agent crash between stages 2 and 4.
        """
        if self.use_mock:
            result = self._mock_existing_pid(segment)
            logger.info(
                f"[CBOS][MOCK] segment={segment} api=getdropdown(EXISTINGPROCESSID) "
                f"| found={result.found} pid={result.process_id}"
            )
            return result

        url = f"{self.process_url}/v1/api/brokerage/getdropdown"
        payload = {
            "TAG": "EXISTINGPROCESSID",
            "LOGINID": login_id,
            "FILTER1": segment,
            "FILTER2": trade_date.isoformat(),
            "extraoption2": "",
            "extraoption3": "",
        }
        logger.info(
            f"[CBOS] segment={segment} api=getdropdown(EXISTINGPROCESSID) "
            f"date={trade_date} | POST {url}"
        )

        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                body = resp.text[:2000]
                if resp.status_code != 200:
                    logger.error(
                        f"[CBOS] segment={segment} api=getdropdown(EXISTINGPROCESSID) "
                        f"| HTTP {resp.status_code} elapsed_ms={elapsed_ms}"
                    )
                    return ExistingProcessResult(
                        found=False, raw_body=body,
                        error=f"HTTP {resp.status_code}",
                        is_transient=resp.status_code >= 500,
                    )
                data = _json.loads(body)
                items = data.get("Result", [])
                if not items:
                    logger.info(
                        f"[CBOS] segment={segment} api=getdropdown(EXISTINGPROCESSID) "
                        f"| found=False (no results) elapsed_ms={elapsed_ms}"
                    )
                    return ExistingProcessResult(found=False, raw_body=body)
                last = items[-1]
                pid = str(last.get("_KEY", ""))
                if not pid:
                    return ExistingProcessResult(found=False, raw_body=body)
                logger.info(
                    f"[CBOS] segment={segment} api=getdropdown(EXISTINGPROCESSID) "
                    f"| found=True pid={pid} elapsed_ms={elapsed_ms}"
                )
                return ExistingProcessResult(
                    found=True,
                    process_id=pid,
                    description=last.get("_DESC", ""),
                    raw_body=body,
                )
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            logger.error(
                f"[CBOS] segment={segment} api=getdropdown(EXISTINGPROCESSID) "
                f"| EXCEPTION elapsed_ms={elapsed_ms} error={exc}"
            )
            return ExistingProcessResult(found=False, error=str(exc), is_transient=True)

    # -------------------------------------------------------------------------
    # 6. MTF operations chain (v2 doc steps 13, 15, 17, 19/20, 22, 24)
    #    All four use LOGINID="G_LID" (hardcoded, see utils/constants.py) and
    #    DD-MMM-YYYY dates. Fired after GTG checks (reusing file_process_status
    #    with process names: CollateralValuation, CollateralAllocation,
    #    FundTransfer, EARLYPAYIN, WEEKLYAUTOCLOSURE) return TRUE.
    # -------------------------------------------------------------------------

    @otel_trace
    async def get_collateral_valuation(self, login_id: str, margin_date: str) -> MtfTriggerResult:
        """POST {PROCESS_URL}/v1/api/process/GetCollateralValuation (step 13)."""
        if self.use_mock:
            return self._mock_mtf_trigger("GetCollateralValuation")

        url = f"{self.process_url}/v1/api/process/GetCollateralValuation"
        payload = {
            "BUTTONNAME": "COLLATERAL_VALUATION_DATEWISE",
            "LOGINID": login_id,
            "MARGINDATE": margin_date,
        }
        return await self._post_mtf_trigger(url, payload, "GetCollateralValuation")

    @otel_trace
    async def mtf_collateral_allocation(self, login_id: str, trade_date: str) -> MtfTriggerResult:
        """POST {PROCESS_URL}/v1/api/process/MTFTradeProcessCollateralAllocation (step 15)."""
        if self.use_mock:
            return self._mock_mtf_trigger("MTFTradeProcessCollateralAllocation")

        url = f"{self.process_url}/v1/api/process/MTFTradeProcessCollateralAllocation"
        payload = {"LOGINID": login_id, "TRADEDATE": trade_date}
        return await self._post_mtf_trigger(url, payload, "MTFTradeProcessCollateralAllocation")

    @otel_trace
    async def mtf_fund_transfer(self, login_id: str, trade_date: str) -> MtfTriggerResult:
        """POST {PROCESS_URL}/v1/api/process/MTFTradeProcessFundTransfer (step 17)."""
        if self.use_mock:
            return self._mock_mtf_trigger("MTFTradeProcessFundTransfer")

        url = f"{self.process_url}/v1/api/process/MTFTradeProcessFundTransfer"
        payload = {"LOGINID": login_id, "TRADEDATE": trade_date}
        return await self._post_mtf_trigger(url, payload, "MTFTradeProcessFundTransfer")

    @otel_trace
    async def mtf_trade_process(self, login_id: str, trade_date: str, type_: str) -> MtfTriggerResult:
        """
        POST {PROCESS_URL}/v1/api/process/MTFTradeProcess (steps 19, 20, 22, 24)
        type_ one of: "BUY PROCESS" | "BUY POSTING" | "SELL PROCESS AND POSTING" | "WEEKLY AUTOCLOSURE"
        """
        if self.use_mock:
            return self._mock_mtf_trigger(f"MTFTradeProcess({type_})")

        url = f"{self.process_url}/v1/api/process/MTFTradeProcess"
        payload = {"LOGINID": login_id, "TRADEDATE": trade_date, "TYPE": type_}
        return await self._post_mtf_trigger(url, payload, f"MTFTradeProcess({type_})")

    async def _post_mtf_trigger(self, url: str, payload: dict, api_name: str) -> MtfTriggerResult:
        """Shared POST + parse logic for all fire-and-forget MTF trigger calls."""
        logger.info(f"[CBOS] api={api_name} | POST {url}")
        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                body = resp.text[:2000]
                if resp.status_code != 200:
                    logger.error(f"[CBOS] api={api_name} | HTTP {resp.status_code} elapsed_ms={elapsed_ms}")
                    return MtfTriggerResult(
                        success=False, raw_body=body, http_status=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                        is_transient=resp.status_code >= 500,
                    )
                message = _parse_mtf_message(body)
                logger.info(f"[CBOS] api={api_name} | message={message!r} elapsed_ms={elapsed_ms}")
                return MtfTriggerResult(success=True, message=message, raw_body=body)
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            logger.error(f"[CBOS] api={api_name} | EXCEPTION elapsed_ms={elapsed_ms} error={exc}")
            return MtfTriggerResult(success=False, error=str(exc), is_transient=True)

    # =========================================================================
    # Mock implementations — used when use_mock=True
    # =========================================================================

    def _mock_file_status(self, segment: str, process_name: str) -> FileStatusResult:
        """
        Simulates file_process_status responses.
        - BeginFileUpload returns SKIP for segments whose code contains "SKIP"
        - All other polls return FALSE until _mock_ready_after calls, then TRUE
        """
        if "SKIP" in segment.upper() and process_name == "BeginFileUpload":
            return FileStatusResult(
                response="SKIP",
                raw_body='{"Status":"Success","Data":[{"MSG":"SKIP"}]}',
            )

        key = f"{segment}_{process_name}"
        self._mock_call_counts[key] = self._mock_call_counts.get(key, 0) + 1

        if self._mock_call_counts[key] >= self._mock_ready_after:
            return FileStatusResult(
                response="TRUE",
                raw_body='{"Status":"Success","Data":[{"MSG":"TRUE"}]}',
            )
        return FileStatusResult(
            response="FALSE",
            raw_body='{"Status":"Success","Data":[{"MSG":"FALSE"}]}',
        )

    def _mock_new_trade_process(
        self, group_name: str, process_id: str
    ) -> NewTradeProcessResult:
        """
        Simulates getNewTradeProcess.
        Derives a stable fake PROCESSID from the segment name so the same
        segment always gets the same ID within a test run.
        """
        if process_id == "0":
            fake_pid = str(
                int(hashlib.md5(group_name.encode()).hexdigest()[:6], 16) % 90000 + 10000
            )
        else:
            fake_pid = process_id

        body = _json.dumps({
            "Status": "Success",
            "Result": {
                "Table1": [{"PROCESSID": int(fake_pid), "ISRUNNABLE": True, "ISAUTOUPLOAD": True}],
                "Table2": [],
            },
        })
        return NewTradeProcessResult(
            success=True,
            process_id=fake_pid,
            is_runnable=True,
            is_auto_upload=True,
            raw_body=body,
        )

    def _mock_existing_pid(self, segment: str) -> ExistingProcessResult:
        """Simulates getdropdown EXISTINGPROCESSID — returns the same fake PID."""
        fake_pid = str(
            int(hashlib.md5(segment.encode()).hexdigest()[:6], 16) % 90000 + 10000
        )
        return ExistingProcessResult(
            found=True,
            process_id=fake_pid,
            description=f"{fake_pid} - CV0001 - Mock Entry",
        )

    def _mock_mtf_trigger(self, api_name: str) -> MtfTriggerResult:
        """Simulates any MTF trigger call — always succeeds immediately."""
        message = "Process completed successfully"
        logger.info(f"[CBOS][MOCK] api={api_name} | message={message!r}")
        return MtfTriggerResult(success=True, message=message, raw_body=_json.dumps({
            "Status": "Success", "Result": [{"MSG": message}],
        }))

    # -------------------------------------------------------------------------
    # Mock tuning helpers (useful in tests / local runs)
    # -------------------------------------------------------------------------

    def mock_set_ready_after(self, n: int) -> None:
        """Set how many polls must occur before file_process_status returns TRUE."""
        self._mock_ready_after = n

    def mock_reset_counts(self) -> None:
        """Reset all poll counters (useful between test cases)."""
        self._mock_call_counts.clear()


# =============================================================================
# Response parsers
# =============================================================================

def _parse_msg(body: str) -> str:
    """
    Parse the MSG value from a file_process_status response.
    Expected: {"Status":"Success","Data":[{"MSG":"TRUE"}]}
    Falls back to string search if JSON parsing fails.
    """
    try:
        data = _json.loads(body)
        msg = data["Data"][0]["MSG"]
        return msg.upper() if msg else "FALSE"
    except Exception:
        upper = body.upper()
        for val in ("SKIP", "TRUE", "FALSE"):
            if val in upper:
                return val
        return "FALSE"


def _parse_new_trade_process(body: str) -> NewTradeProcessResult:
    """Parse the getNewTradeProcess response and extract PROCESSID + step list."""
    try:
        data = _json.loads(body)
        if data.get("Status") != "Success":
            return NewTradeProcessResult(
                success=False,
                raw_body=body,
                error=f"CBOS Status={data.get('Status')}",
            )
        result = data.get("Result", {})
        table1 = result.get("Table1", [{}])
        table2 = result.get("Table2", [])

        t1 = table1[0] if table1 else {}
        pid = str(t1["PROCESSID"]) if t1.get("PROCESSID") else None

        steps = [
            NewTradeProcessStep(
                id=row.get("ID", 0),
                step_no=row.get("STEPNO", 0),
                name=row.get("NAME", ""),
                status=row.get("STATUS", "PENDING"),
                status_desc=row.get("STATUSDESC"),
                upload_id=row.get("UPLOADID", 0),
                start_datetime=row.get("STARTDATETIME"),
                end_datetime=row.get("ENDDATETIME"),
            )
            for row in table2
        ]

        return NewTradeProcessResult(
            success=True,
            process_id=pid,
            is_runnable=bool(t1.get("ISRUNNABLE", False)),
            is_auto_upload=bool(t1.get("ISAUTOUPLOAD", False)),
            steps=steps,
            raw_body=body,
        )
    except Exception as exc:
        return NewTradeProcessResult(success=False, raw_body=body, error=str(exc))


def _parse_mtf_message(body: str) -> str:
    """
    Parse the human-readable message from an MTF trigger response.
    Handles both shapes seen in the API doc:
      {"Status":"Success","Result":{"Table1":[{"MSG":"..."}]}}
      {"Status":"Success","Result":[{"MSG":"..."}]}
    """
    try:
        data = _json.loads(body)
        result = data.get("Result")
        if isinstance(result, dict):
            rows = result.get("Table1", [])
        elif isinstance(result, list):
            rows = result
        else:
            rows = []
        if rows and isinstance(rows[0], dict):
            return rows[0].get("MSG") or rows[0].get("Result") or ""
        return ""
    except Exception:
        return body[:200]
