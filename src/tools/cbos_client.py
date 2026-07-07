"""
CBOS HTTP client — exact MOFSL API contract.

Two separate base URLs:
  STATUS_URL  (port 8087) — Good-to-Go / completion checks via file_process_status
  PROCESS_URL (port 8003) — process management (get-or-reserve PID, trigger)

Segment pipeline — identical for all 7 segments (CASH/EQ, F&O/DR, CD/CUR,
SLBM/SL, MCX, NCDEX, MTF); MTF is not special-cased, it runs through the same
7 steps:
  1. file_process_status(BeginFileUpload)        → holiday check
  2. getdropdown(EXISTINGPROCESSID); if not found,
     get_new_trade_process(PROCESSID="0")        → get-or-reserve process_id
  3. file_process_status(FILEUPLOAD)             → poll until exchange files uploaded
  4. get_new_trade_process(PROCESSID=<actual>)   → trigger billing processing (once)
  5. file_process_status(BILLPOSTING)            → poll until bill posting done
  6. file_process_status(RECON)                  → poll until reconciliation done
  7. file_process_status(CONTRACTNOTEGENERATION) → poll until contract notes done

ProcessName values for file_process_status (segment pipeline):
  BeginFileUpload        — holiday gate (SKIP = holiday, TRUE = go, FALSE = not open yet)
  FILEUPLOAD             — all exchange files received gate
  BILLPOSTING            — billing calculations complete
  RECON                  — reconciliation complete
  CONTRACTNOTEGENERATION — contract note generation complete

Post-trade pipeline (T+1) — 5 processes, run once per trade_date after all 7
segments, each through the same 3-step GTG → trigger → confirm pattern:
  1. Collateral Valuation   (COLVAL)   → trigger_collateral_valuation()
  2. Collateral Allocation  (COLALLOC) → trigger_collateral_allocation()
  3. MTF Fund Transfer      (MTFFT)    → trigger_mtf_fund_transfer()
  4. Daily Margin Reporting (DMRPT)    → trigger_daily_margin_reporting()
  5. Daily Margin Statements(DMSTMT)   → trigger_daily_margin_statements()
Each uses file_process_status(Segment=<code>, ProcessName=<see
utils/constants.POST_TRADE_GTG_PROCESS_NAME>) for both the pre-trigger GTG
poll and the post-trigger confirm poll.

CBOS API request / response shapes
  file_process_status
    POST {STATUS_URL}/api/edp/file_process_status
    Body: {"Segment":"EQ","ProcessName":"BeginFileUpload","UserID":"CV0001"}
    OK:   {"Status":"Success","Data":[{"MSG":"TRUE|FALSE|SKIP"}]}

  getdropdown EXISTINGPROCESSID  (Step 2 — check before reserving)
    POST {PROCESS_URL}/v1/api/brokerage/getdropdown
    Body: {"TAG":"EXISTINGPROCESSID","LOGINID":"CV0001","FILTER1":"EQ","FILTER2":"2026-06-29","extraoption2":"","extraoption3":""}
    OK:   {"Status":"Success","Result":[{"_KEY":17658,"_DESC":"17658 - CV0001 - Jun 29 2026 2:19PM"}]}
    Empty Result → no process ID exists yet; reserve a new one.

  getNewTradeProcess  (reserve → PROCESSID="0", trigger → PROCESSID=actual)
    POST {PROCESS_URL}/v1/api/process/getNewTradeProcess
    Body: {"GROUPNAME":"EQ","LOGINID":"CV0001","TRADEDATE":"2026-06-29","PROCESSID":"0"}
    OK:   {"Status":"Success","Result":{"Table1":[{"PROCESSID":17658,"ISRUNNABLE":true,...}],"Table2":[...]}}

  Post-trade triggers (DD-Mon-YYYY date format, e.g. "29-Jun-2026")
    POST {PROCESS_URL}/v1/api/process/GetCollateralValuation
      Body: {"BUTTONNAME":"COLLATERAL_VALUATION_DATEWISE","LOGINID":"G_LID","MARGINDATE":"29-Jun-2026"}
    POST {PROCESS_URL}/v1/api/process/MTFTradeProcessCollateralAllocation
    POST {PROCESS_URL}/v1/api/process/MTFTradeProcessFundTransfer
    POST {PROCESS_URL}/v1/api/process/DailyMarginReporting
    POST {PROCESS_URL}/v1/api/process/DailyMarginStatements
      Body (last 4): {"LOGINID":"G_LID","TRADEDATE":"29-Jun-2026"}
    OK: {"Status":"Success","Data":[{"MSG":"Process started successfully"}]}
"""

from __future__ import annotations

import itertools
import json as _json
import time as _time
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

import httpx

from cams_otel_lib import Logger as logger, otel_trace


def _classify_exception(exc: Exception) -> str:
    """
    Distinguishes a CBOS call timing out from other network failures, so
    logs/status can tell "CBOS is slow/unresponsive" apart from "CBOS is
    unreachable" apart from some other client-side bug — all three used to
    collapse into one generic "EXCEPTION" error string.
    """
    if isinstance(exc, httpx.TimeoutException):
        return "CBOS_TIMEOUT"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "CBOS_UNREACHABLE"
    return "CBOS_CLIENT_ERROR"


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
    """
    One processing step from Table2 of the getNewTradeProcess response.

    Used by pipeline.stages._recover_trigger() to decide whether CBOS
    already received an earlier, unconfirmed trigger call: any step with
    status IN_PROGRESS or SUCCESS means it did (do not re-trigger); all
    steps PENDING (or an empty Table2) means it didn't (safe to re-trigger).
    """
    id: int
    step_no: int
    name: str
    status: str                # "PENDING" | "IN_PROGRESS" | "SUCCESS" | "FAILED"
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
    """
    Result of a getdropdown EXISTINGPROCESSID call — used for Step 2
    (get-or-reserve process_id) on every segment, and doubles as the
    crash-recovery lookup if process_id was lost from the DB row.
    """
    found: bool
    process_id: Optional[str] = None
    description: Optional[str] = None
    raw_body: str = ""
    error: Optional[str] = None
    is_transient: bool = False


@dataclass
class PostTradeTriggerResult:
    """
    Result of one of the 5 T+1 post-trade trigger calls (GetCollateralValuation
    / MTFTradeProcessCollateralAllocation / MTFTradeProcessFundTransfer /
    DailyMarginReporting / DailyMarginStatements). Unlike the segment trigger
    (getNewTradeProcess), there's no process_id involved — just an
    acknowledgement message that the job was started.
    """
    success: bool
    message: str = ""
    raw_body: str = ""
    http_status: int = 200
    error: Optional[str] = None
    is_transient: bool = False


def to_ddmmmyyyy(d: date) -> str:
    """Format a date as CBOS expects for post-trade trigger calls, e.g. '29-Jun-2026'."""
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
        # (segment, trade_date_iso) -> reserved PROCESSID — lets the mock
        # correctly answer Step 2's "does a process ID already exist?"
        # check (getdropdown) based on whether one was actually reserved
        # yet, instead of always deterministically claiming "found".
        self._mock_reserved_pids: dict[tuple[str, str], str] = {}
        self._mock_pid_counter = itertools.count(17001)
        # (segment, trade_date_iso) -> count of trigger-mode calls
        # (PROCESSID != "0") made so far — lets the mock simulate Table2
        # progressing from "nothing started" (1st call) to "IN_PROGRESS"
        # (2nd+ call), so tests can exercise the TRIGGERING recovery
        # decision tree in pipeline.stages.handle_trigger() realistically.
        self._mock_trigger_calls: dict[tuple[str, str], int] = {}

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
                if msg.startswith("ERROR:"):
                    logger.error(
                        f"[CBOS] segment={segment} api=file_process_status process={process_name} "
                        f"| CBOS rejected request status={msg} elapsed_ms={elapsed_ms}"
                    )
                    return FileStatusResult(
                        response="FALSE",
                        raw_body=body,
                        http_status=200,
                        error=msg,
                        is_transient=False,
                    )
                logger.info(
                    f"[CBOS] segment={segment} api=file_process_status process={process_name} "
                    f"| response={msg} elapsed_ms={elapsed_ms}"
                )
                return FileStatusResult(response=msg, raw_body=body, http_status=200)
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            category = _classify_exception(exc)
            logger.warning(
                f"[CBOS] segment={segment} api=file_process_status process={process_name} "
                f"| {category} elapsed_ms={elapsed_ms} error={exc}"
            )
            return FileStatusResult(response="FALSE", error=f"{category}: {exc}", is_transient=True)

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
            result = self._mock_new_trade_process(group_name, trade_date, process_id)
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
            category = _classify_exception(exc)
            logger.warning(
                f"[CBOS] segment={group_name} api=getNewTradeProcess mode={mode} "
                f"| {category} elapsed_ms={elapsed_ms} error={exc}"
            )
            return NewTradeProcessResult(success=False, error=f"{category}: {exc}", is_transient=True)

    # -------------------------------------------------------------------------
    # 3. get_existing_process_id — Step 2 "get-or-reserve" check
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

        Step 2 of the segment pipeline: always called first, before deciding
        whether to reserve a new process_id. If a PROCESSID already exists
        for this segment+date (e.g. RPA reserved one already, or the agent
        itself reserved one on an earlier cycle), it is reused as-is instead
        of reserving a second one. Also doubles as the crash-recovery lookup
        if process_id was somehow lost from the DB row before TRIGGER.
        """
        if self.use_mock:
            result = self._mock_existing_pid(segment, trade_date)
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
            category = _classify_exception(exc)
            logger.warning(
                f"[CBOS] segment={segment} api=getdropdown(EXISTINGPROCESSID) "
                f"| {category} elapsed_ms={elapsed_ms} error={exc}"
            )
            return ExistingProcessResult(found=False, error=f"{category}: {exc}", is_transient=True)

    # -------------------------------------------------------------------------
    # 5. Post-trade (T+1) triggers — Collateral Valuation / Allocation,
    #    MTF Fund Transfer, Daily Margin Reporting / Statements
    # -------------------------------------------------------------------------

    @otel_trace
    async def trigger_collateral_valuation(
        self, login_id: str, margin_date: date,
    ) -> PostTradeTriggerResult:
        """POST {PROCESS_URL}/v1/api/process/GetCollateralValuation — post-trade Process 1."""
        payload = {
            "BUTTONNAME": "COLLATERAL_VALUATION_DATEWISE",
            "LOGINID": login_id,
            "MARGINDATE": to_ddmmmyyyy(margin_date),
        }
        return await self._post_trade_trigger(
            "GetCollateralValuation", payload, segment="COLVAL",
        )

    @otel_trace
    async def trigger_collateral_allocation(
        self, login_id: str, trade_date: date,
    ) -> PostTradeTriggerResult:
        """POST {PROCESS_URL}/v1/api/process/MTFTradeProcessCollateralAllocation — post-trade Process 2."""
        return await self._trigger_post_trade_job(
            "MTFTradeProcessCollateralAllocation", login_id, trade_date, segment="COLALLOC",
        )

    @otel_trace
    async def trigger_mtf_fund_transfer(
        self, login_id: str, trade_date: date,
    ) -> PostTradeTriggerResult:
        """POST {PROCESS_URL}/v1/api/process/MTFTradeProcessFundTransfer — post-trade Process 3."""
        return await self._trigger_post_trade_job(
            "MTFTradeProcessFundTransfer", login_id, trade_date, segment="MTFFT",
        )

    @otel_trace
    async def trigger_daily_margin_reporting(
        self, login_id: str, trade_date: date,
    ) -> PostTradeTriggerResult:
        """POST {PROCESS_URL}/v1/api/process/DailyMarginReporting — post-trade Process 4."""
        return await self._trigger_post_trade_job(
            "DailyMarginReporting", login_id, trade_date, segment="DMRPT",
        )

    @otel_trace
    async def trigger_daily_margin_statements(
        self, login_id: str, trade_date: date,
    ) -> PostTradeTriggerResult:
        """POST {PROCESS_URL}/v1/api/process/DailyMarginStatements — post-trade Process 5."""
        return await self._trigger_post_trade_job(
            "DailyMarginStatements", login_id, trade_date, segment="DMSTMT",
        )

    async def _trigger_post_trade_job(
        self, endpoint_name: str, login_id: str, trade_date: date, segment: str,
    ) -> PostTradeTriggerResult:
        """Shared body shape for the 4 post-trade endpoints that take {LOGINID, TRADEDATE}."""
        payload = {"LOGINID": login_id, "TRADEDATE": to_ddmmmyyyy(trade_date)}
        return await self._post_trade_trigger(endpoint_name, payload, segment=segment)

    async def _post_trade_trigger(
        self, endpoint_name: str, payload: dict, segment: str,
    ) -> PostTradeTriggerResult:
        if self.use_mock:
            result = self._mock_post_trade_trigger()
            logger.info(
                f"[CBOS][MOCK] segment={segment} api={endpoint_name} "
                f"| success={result.success} message={result.message}"
            )
            return result

        url = f"{self.process_url}/v1/api/process/{endpoint_name}"
        logger.info(f"[CBOS] segment={segment} api={endpoint_name} | POST {url}")

        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                body = resp.text[:2000]
                if resp.status_code != 200:
                    logger.error(
                        f"[CBOS] segment={segment} api={endpoint_name} "
                        f"| HTTP {resp.status_code} elapsed_ms={elapsed_ms}"
                    )
                    return PostTradeTriggerResult(
                        success=False,
                        raw_body=body,
                        http_status=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                        is_transient=resp.status_code >= 500,
                    )
                success, message = _parse_post_trade_trigger(body)
                if not success:
                    logger.error(
                        f"[CBOS] segment={segment} api={endpoint_name} "
                        f"| CBOS rejected request message={message} elapsed_ms={elapsed_ms}"
                    )
                    return PostTradeTriggerResult(
                        success=False, message=message, raw_body=body, error=message,
                    )
                logger.info(
                    f"[CBOS] segment={segment} api={endpoint_name} "
                    f"| message={message} elapsed_ms={elapsed_ms}"
                )
                return PostTradeTriggerResult(success=True, message=message, raw_body=body)
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            category = _classify_exception(exc)
            logger.warning(
                f"[CBOS] segment={segment} api={endpoint_name} "
                f"| {category} elapsed_ms={elapsed_ms} error={exc}"
            )
            return PostTradeTriggerResult(success=False, error=f"{category}: {exc}", is_transient=True)

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
        self, group_name: str, trade_date: date, process_id: str
    ) -> NewTradeProcessResult:
        """
        Simulates getNewTradeProcess.

        process_id="0" (reserve): allocates the next incrementing fake PID
        and records it keyed by (segment, trade_date) so a subsequent
        get_existing_process_id() call for the same segment+date correctly
        reports "found" — mirrors real CBOS behaviour where a PROCESSID,
        once reserved, persists for that segment+date.

        process_id=<actual> (trigger / recovery-check): the 1st call for a
        given (segment, trade_date) returns an empty Table2 (nothing has
        started running yet — matches the state CBOS would be in right
        after just receiving the call, or the state a recovery check would
        see if CBOS never got a prior call at all). Every call after that
        returns Table2 with one step IN_PROGRESS, simulating that CBOS is
        now actively executing that PROCESSID — this is what lets tests
        exercise both branches of the TRIGGERING recovery decision tree in
        pipeline.stages.handle_trigger().
        """
        key = (group_name.upper(), trade_date.isoformat())
        if process_id == "0":
            if key not in self._mock_reserved_pids:
                self._mock_reserved_pids[key] = str(next(self._mock_pid_counter))
            fake_pid = self._mock_reserved_pids[key]
            table2 = []
        else:
            fake_pid = process_id
            call_no = self._mock_trigger_calls.get(key, 0) + 1
            self._mock_trigger_calls[key] = call_no
            if call_no == 1:
                table2 = []
            else:
                table2 = [{
                    "ID": 1, "STEPNO": 1, "NAME": "TRADE_MERGER",
                    "STATUS": "IN_PROGRESS", "STATUSDESC": None, "UPLOADID": 0,
                    "STARTDATETIME": None, "ENDDATETIME": None,
                }]

        body = _json.dumps({
            "Status": "Success",
            "Result": {
                "Table1": [{"PROCESSID": int(fake_pid), "ISRUNNABLE": True, "ISAUTOUPLOAD": True}],
                "Table2": table2,
            },
        })
        return NewTradeProcessResult(
            success=True,
            process_id=fake_pid,
            is_runnable=True,
            is_auto_upload=True,
            steps=_parse_new_trade_process(body).steps,
            raw_body=body,
        )

    def _mock_existing_pid(self, segment: str, trade_date: date) -> ExistingProcessResult:
        """
        Simulates getdropdown EXISTINGPROCESSID.

        Only reports "found" if a PID has actually been reserved for this
        (segment, trade_date) via _mock_new_trade_process — before that, it
        correctly reports not-found (empty Result), same as real CBOS, so
        Step 2's get-or-reserve branch is faithfully exercised in mock mode.
        """
        key = (segment.upper(), trade_date.isoformat())
        pid = self._mock_reserved_pids.get(key)
        if not pid:
            return ExistingProcessResult(found=False, raw_body='{"Status":"Success","Result":[]}')
        return ExistingProcessResult(
            found=True,
            process_id=pid,
            description=f"{pid} - CV0001 - Mock Entry",
        )

    def _mock_post_trade_trigger(self) -> PostTradeTriggerResult:
        """Post-trade triggers always succeed deterministically in mock mode."""
        return PostTradeTriggerResult(
            success=True,
            message="Process started successfully",
            raw_body='{"Status":"Success","Data":[{"MSG":"Process started successfully"}]}',
        )

    # -------------------------------------------------------------------------
    # Mock tuning helpers (useful in tests / local runs)
    # -------------------------------------------------------------------------

    def mock_set_ready_after(self, n: int) -> None:
        """Set how many polls must occur before file_process_status returns TRUE."""
        self._mock_ready_after = n

    def mock_reset_counts(self) -> None:
        """Reset all poll counters and reserved PIDs (useful between test cases)."""
        self._mock_call_counts.clear()
        self._mock_reserved_pids.clear()
        self._mock_trigger_calls.clear()


# =============================================================================
# Response parsers
# =============================================================================

def _parse_msg(body: str) -> str:
    """
    Parse the MSG value from a file_process_status response.
    Expected: {"Status":"Success","Data":[{"MSG":"TRUE"}]}
    Falls back to string search if JSON parsing fails.

    A non-"Success" top-level Status means CBOS rejected/errored the request
    (e.g. bad segment/process name) -- this must NOT be silently treated as
    "not ready yet" (FALSE), since that would cause the poller to retry
    forever instead of surfacing the error. It is reported as an explicit
    error string so callers can flag it.
    """
    try:
        data = _json.loads(body)
        if data.get("Status") and data.get("Status") != "Success":
            return f"ERROR:{data.get('Status')}"
        msg = data["Data"][0]["MSG"]
        return msg.upper() if msg else "FALSE"
    except Exception:
        upper = body.upper()
        for val in ("SKIP", "TRUE", "FALSE"):
            if val in upper:
                return val
        return "FALSE"


def _parse_post_trade_trigger(body: str) -> tuple[bool, str]:
    """
    Parse a post-trade trigger response body. Expected shape:
      {"Status":"Success","Data":[{"MSG":"Process started successfully"}]}
    A non-"Success" top-level Status means CBOS rejected the request.
    Falls back to treating any unparsable-but-200 body as success (some of
    these endpoints — e.g. MTF Fund Transfer — just "return a job execution
    result" per the spec, with no guaranteed JSON shape).
    """
    try:
        data = _json.loads(body)
        status = data.get("Status")
        if status and status != "Success":
            return False, f"CBOS Status={status}"
        msg = None
        items = data.get("Data")
        if isinstance(items, list) and items:
            msg = items[0].get("MSG")
        if not msg:
            msg = data.get("MSG") or data.get("Message")
        return True, msg or "Process started successfully"
    except Exception:
        return True, (body[:200] if body else "Process started successfully")


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
