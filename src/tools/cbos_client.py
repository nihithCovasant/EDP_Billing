"""
CBOS HTTP client — exact MOFSL API contract.

Two separate base URLs:
  STATUS_URL  (port 8087) — Good-to-Go / completion checks via file_process_status
  PROCESS_URL (port 8003) — process management (read PID, trigger)

Segment pipeline — identical for all 9 segments (CASH/EQ, F&O/DR, CD/CUR,
SLB, NCDEX, NCDEXPHY, MCX, MCXPHY, NSECOM), 7 steps:
  1. file_process_status(BeginFileUpload)        → holiday check
  2. getdropdown(EXISTINGPROCESSID)              → READ the uploader-reserved
     process_id (the uploader is the sole reserver — a miss means "wait",
     never "mint one"; see RealSegmentStateMachine's module docstring)
  3. file_process_status(FILEUPLOAD)             → poll until exchange files uploaded
  4. get_new_trade_process(PROCESSID=<actual>)   → trigger billing processing (once)
  5. file_process_status(BILLPOSTING)            → poll until bill posting done
  6. file_process_status(RECON)                  → poll until reconciliation done
  7. file_process_status(CONTRACTNOTEGENERATION) → poll until contract notes done

Post-trade pipeline (T+1) — 5 processes, run once per trade_date after all
segments, each through WAITING_FOR_GTG -> [TRIGGERED ->] WAITING_FOR_COMPLETION:
  1. Collateral Valuation   (COLVAL)   → trigger_collateral_valuation()
  2. Collateral Allocation  (COLALLOC) → trigger_collateral_allocation()
  3. MTF Fund Transfer      (MTFFT)    → trigger_mtf_fund_transfer()
  4. Daily Margin Reporting (DMRPT)    → trigger_daily_margin_reporting()
  5. Daily Margin Statements(DMSTMT)   → trigger_daily_margin_statements()
Each has a matching check_*_triggered() "already triggered" pre-check at
WAITING_FOR_GTG, so a resumed pod never double-fires a trigger: REFRESH-variant
calls to the trigger endpoint itself for COLVAL/DMRPT, file_process_status
with a dedicated ProcessName for COLALLOC/MTFFT/DMSTMT.

CBOS API request / response shapes
  file_process_status  (V5: TradeDate is REQUIRED in every call — Shape A
    carries Segment, Shape B (post-trade checks) does not; payload builders
    live in edpb_core.cbos, shared with the uploader)
    POST {STATUS_URL}/api/edp/file_process_status
    Body: {"Segment":"EQ","TradeDate":"2026-06-29","ProcessName":"BeginFileUpload","UserID":"CV0001"}
    OK:   {"Status":"Success","Data":[{"MSG":"TRUE|FALSE|SKIP"}]}

    EXCEPTION: the 3 "already triggered" ProcessNames reused via this same
    endpoint (MTFCOLLALLOC / MTFFUNDTRAN / CHECKDAILYMARGINSTATEMENT) return
    a full sentence in MSG, not TRUE/FALSE/SKIP (confirmed against
    EDP_Trade_Process_API_v3 steps 20/23/37):
      MTFCOLLALLOC / MTFFUNDTRAN:  {"MSG":"PROCESS TRIGGERED IS PENDING"}
      CHECKDAILYMARGINSTATEMENT:   {"MSG":"DAILYMARGINSTATEMENT IS NOT TRIGGERED"}
    Both mean NOT yet triggered — see _parse_already_triggered_sentence().
    FileStatusResult.is_ready (strict "== TRUE") must NOT be used for these
    3, or it would always read "not yet triggered" regardless of CBOS's
    actual state, defeating the double-trigger guard entirely.

  getdropdown EXISTINGPROCESSID  (Step 2 — read the uploader-reserved PID)
    POST {PROCESS_URL}/v1/api/brokerage/getdropdown
    Body: {"TAG":"EXISTINGPROCESSID","LOGINID":"CV0001","FILTER1":"EQ","FILTER2":"2026-06-29","extraoption2":"","extraoption3":""}
    OK:   {"Status":"Success","Result":[{"_KEY":17658,"_DESC":"17658 - CV0001 - Jun 29 2026 2:19PM"}]}
    Empty Result → uploader hasn't reserved yet; wait and re-check next cycle.

  getNewTradeProcess  (trigger → PROCESSID=actual; PROCESSID="0" reserve mode
    is the UPLOADER's call, kept here only for tests playing the uploader)
    POST {PROCESS_URL}/v1/api/process/getNewTradeProcess
    Body: {"GROUPNAME":"EQ","LOGINID":"CV0001","TRADEDATE":"2026-06-29","PROCESSID":"0"}
    OK:   {"Status":"Success","Result":{"Table1":[{"PROCESSID":17658,"ISRUNNABLE":true,...}],"Table2":[...]}}

  Post-trade triggers (DD-Mon-YYYY date format, e.g. "29-Jun-2026")
    POST {PROCESS_URL}/v1/api/process/GetCollateralValuation
      Body: {"BUTTONNAME":"COLLATERAL_VALUATION_DATEWISE","LOGINID":"G_LID","MARGINDATE":"29-Jun-2026"}
    POST {PROCESS_URL}/v1/api/process/CombinedMarginProcess  (DMRPT — same
      BUTTONNAME-driven shape, NOT the {LOGINID,TRADEDATE}-only shape below)
      Body: {"BUTTONNAME":"COMBINEDMARGIN_PROCESS","LOGINID":"G_LID","MARGINDATE":"29-Jun-2026"}
    POST {PROCESS_URL}/v1/api/process/MTFTradeProcessCollateralAllocation
    POST {PROCESS_URL}/v1/api/process/MTFTradeProcessFundTransfer
      Body (both): {"LOGINID":"G_LID","TRADEDATE":"29-Jun-2026"}
    OK: {"Status":"Success","Data":[{"MSG":"Process started successfully"}]}

    DailyMarginStatements (DMSTMT) trigger is the one exception: it's a
    file_process_status call, not a Process API call (confirmed via
    EDP_Trade_Process_API_v3 STEP 38).
    POST {STATUS_URL}/api/edp/file_process_status
      Body: {"Segment":"DMSTMT","ProcessName":"DAILYMARGINSTATEMENT","UserID":"G_LID"}
    OK: {"Status":"Success","Data":[{"MSG":"TRUE"}]}

  Post-trade "already triggered" REFRESH checks (COLVAL/DMRPT only —
  COLALLOC/MTFFT/DMSTMT go through file_process_status instead, see above)
    POST {PROCESS_URL}/v1/api/process/GetCollateralValuation
    POST {PROCESS_URL}/v1/api/process/CombinedMarginProcess
      Body (both): {"BUTTONNAME":"REFRESH","LOGINID":"G_LID"} — literally
      "REFRESH", no MARGINDATE (confirmed against EDP_Trade_Process_API_v3
      steps 17/35). CBOS has no input validation, so a wrong BUTTONNAME/
      extra field tends to be silently accepted and misbehave rather than
      raise a clean error.
    OK: {"Status":"Success","Result":{"Table1":[...]}} — non-empty Table1
      means already triggered/running; {"Table1":[]} means not yet.
"""

from __future__ import annotations

import asyncio
import itertools
import json as _json
import time as _time
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

import httpx

from edpb_core.cbos import file_process_status_payload, file_process_status_payload_b

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
    is_transient: bool = False  # True for network errors and HTTP 5xx/429 (retryable)

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

    Used by RealSegmentStateMachine._recover_trigger() to decide whether
    CBOS already received an earlier trigger call: any step IN_PROGRESS or
    SUCCESS means yes (don't re-trigger); all PENDING/empty means no.
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
    is_transient: bool = False  # True for network errors and HTTP 5xx/429 (retryable)


@dataclass
class ExistingProcessResult:
    """Result of a getdropdown EXISTINGPROCESSID call — used for Step 2
    (get-or-reserve process_id), and doubles as the crash-recovery lookup
    if process_id was lost from the DB row."""
    found: bool
    process_id: Optional[str] = None
    description: Optional[str] = None
    raw_body: str = ""
    error: Optional[str] = None
    is_transient: bool = False


@dataclass
class AlreadyTriggeredResult:
    """
    Result of an "already triggered" pre-check for a post-trade process,
    used at WAITING_FOR_GTG to decide between the direct edge to
    WAITING_FOR_COMPLETION (already_triggered=True) or TRIGGERED
    (already_triggered=False) — prevents a resumed pod double-firing.
    """
    already_triggered: bool
    raw_body: str = ""
    error: Optional[str] = None
    is_transient: bool = False


@dataclass
class PostTradeTriggerResult:
    """Result of one of the 5 T+1 post-trade trigger calls. Unlike the
    segment trigger, there's no process_id — just an acknowledgement
    message that the job was started."""
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
        # (segment, trade_date_iso) -> reserved PROCESSID, so getdropdown
        # correctly reports "found" only once one is actually reserved.
        self._mock_reserved_pids: dict[tuple[str, str], str] = {}
        self._mock_pid_counter = itertools.count(17001)
        # (segment, trade_date_iso) -> getdropdown(EXISTINGPROCESSID) lookup
        # count; with _mock_uploader_reserve_delay it simulates the uploader
        # reserving the PID some cycles after the agent starts asking.
        self._mock_existing_pid_lookups: dict[tuple[str, str], int] = {}
        self._mock_uploader_reserve_delay = 0
        # (segment, trade_date_iso) -> trigger-mode call count, so Table2
        # progresses from empty (1st call) to IN_PROGRESS (2nd+), letting
        # tests exercise RealSegmentStateMachine.handle_triggered()'s
        # recovery branches.
        self._mock_trigger_calls: dict[tuple[str, str], int] = {}
        # Post-trade "already triggered" pre-checks default to False;
        # tests opt in via mock_mark_already_triggered().
        self._mock_already_triggered_segments: set[str] = set()

    # -------------------------------------------------------------------------
    # Connectivity check — GET /edp/health (see src/agent/__main__.py)
    # -------------------------------------------------------------------------

    @otel_trace
    async def check_connectivity(self) -> dict:
        """
        Lightweight reachability probe for both CBOS base URLs — a plain GET
        with a short timeout, deliberately NOT a real business call (no
        Segment/ProcessName payload), so a health check can't accidentally
        trigger billing side effects. Any HTTP response (even 404/405 — CBOS
        has no route at the bare base URL) counts as "reachable"; only a
        connection error/timeout counts as unreachable.
        """
        if self.use_mock:
            return {
                "status": "mock",
                "status_url": {"ok": True, "url": self.status_url},
                "process_url": {"ok": True, "url": self.process_url},
            }

        async def _probe(url: str) -> dict:
            t0 = _time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url)
                return {
                    "ok": True,
                    "url": url,
                    "http_status": resp.status_code,
                    "latency_ms": int((_time.monotonic() - t0) * 1000),
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "url": url,
                    "error": str(exc),
                    "latency_ms": int((_time.monotonic() - t0) * 1000),
                }

        status_result, process_result = await asyncio.gather(
            _probe(self.status_url), _probe(self.process_url),
        )
        overall_ok = status_result["ok"] and process_result["ok"]
        return {
            "status": "ok" if overall_ok else "error",
            "status_url": status_result,
            "process_url": process_result,
        }

    # -------------------------------------------------------------------------
    # 1. file_process_status — Good-to-Go / completion checks
    # -------------------------------------------------------------------------

    @otel_trace
    async def file_process_status(
        self,
        segment: str,
        process_name: str,
        user_id: str,
        trade_date: date | str | None = None,
        *,
        include_segment: bool = True,
    ) -> FileStatusResult:
        """
        POST {STATUS_URL}/api/edp/file_process_status
        Returns TRUE (ready), FALSE (not yet), or SKIP (holiday/not applicable).

        V5: TradeDate (YYYY-MM-DD) is REQUIRED in every file_process_status
        call. Shape A (include_segment=True — real-segment steps 1/3/9 and
        the BILLPOSTING/RECON/CONTRACTNOTEGENERATION polls) carries Segment;
        Shape B (include_segment=False — the post-trade GTG/completion/
        already-triggered checks, V5 doc steps 13/15-16/19-20/22-23/30-31/
        37-38) does not. Payload shapes come from edpb_core.cbos so all
        three repos agree by construction. trade_date=None is tolerated only
        for legacy callers and logs loudly — real V5 CBOS may resolve the
        WRONG DAY's process without it.
        """
        if self.use_mock:
            result = self._mock_file_status(segment, process_name)
            logger.info(
                f"[CBOS][MOCK] segment={segment} api=file_process_status "
                f"process={process_name} | response={result.response}"
            )
            return result

        url = f"{self.status_url}/api/edp/file_process_status"
        if trade_date is None:
            logger.error(
                f"[CBOS] file_process_status({process_name}) called WITHOUT trade_date - "
                f"sending legacy v3 payload; V5 CBOS may resolve the wrong day's process"
            )
            payload = {"Segment": segment, "ProcessName": process_name, "UserID": user_id}
        else:
            iso = trade_date if isinstance(trade_date, str) else trade_date.isoformat()
            payload = (
                file_process_status_payload(segment, iso, process_name, user_id)
                if include_segment
                else file_process_status_payload_b(iso, process_name, user_id)
            )
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
                        is_transient=_is_transient_http_status(resp.status_code),
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
                        is_transient=_is_transient_http_status(resp.status_code),
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

        Step 2 of the segment pipeline, called before deciding whether to
        reserve a new process_id: if one already exists for this segment+
        date (e.g. RPA reserved it, or an earlier cycle did), it's reused
        instead of reserving a second one. Also doubles as the
        crash-recovery lookup if process_id was lost from the DB row.
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
                        is_transient=_is_transient_http_status(resp.status_code),
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
        """
        POST {PROCESS_URL}/v1/api/process/CombinedMarginProcess — post-trade
        Process 4. Uses the same BUTTONNAME-driven shape as
        trigger_collateral_valuation(), NOT the {LOGINID, TRADEDATE}-only
        shape the other _trigger_post_trade_job() callers use.
        """
        payload = {
            "BUTTONNAME": "COMBINEDMARGIN_PROCESS",
            "LOGINID": login_id,
            "MARGINDATE": to_ddmmmyyyy(trade_date),
        }
        return await self._post_trade_trigger(
            "CombinedMarginProcess", payload, segment="DMRPT",
        )

    @otel_trace
    async def trigger_daily_margin_statements(
        self, login_id: str, trade_date: date,
    ) -> PostTradeTriggerResult:
        """
        POST {STATUS_URL}/api/edp/file_process_status with
        {"ProcessName":"DAILYMARGINSTATEMENT","UserID":login_id} — confirmed
        against EDP_Trade_Process_API_v3 STEP 38 (response {"MSG":"TRUE"}
        on success). Unlike the other 4 post-trade triggers, this one goes
        through the STATUS API, not the PROCESS API's {LOGINID,TRADEDATE}
        shape. trade_date is accepted but unused, kept only to match every
        other trigger_*()'s signature for PostTradeStateMachine's dispatch.
        """
        result = await self.file_process_status(
            segment="DMSTMT", process_name="DAILYMARGINSTATEMENT", user_id=login_id,
        )
        if result.is_error:
            return PostTradeTriggerResult(
                success=False,
                message=result.error or "",
                raw_body=result.raw_body,
                http_status=result.http_status,
                error=result.error,
                is_transient=result.is_transient,
            )
        return PostTradeTriggerResult(
            success=result.is_ready,
            message=result.response,
            raw_body=result.raw_body,
            http_status=result.http_status,
        )

    # -------------------------------------------------------------------------
    # 6. "Already triggered" pre-checks — called at WAITING_FOR_GTG before
    #    firing a post-trade trigger, so a resumed pod never double-fires one.
    # -------------------------------------------------------------------------

    @otel_trace
    async def check_collateral_valuation_triggered(
        self, login_id: str, margin_date: date,
    ) -> AlreadyTriggeredResult:
        """
        POST {PROCESS_URL}/v1/api/process/GetCollateralValuation with
        BUTTONNAME="REFRESH" and no MARGINDATE (confirmed against
        EDP_Trade_Process_API_v3 step 17). A non-empty Result.Table1 means
        a valuation run for this date already exists. margin_date is
        accepted but unused, kept only to match every check_*_triggered()
        method's (login_id, date) signature for the generic dispatch.
        """
        payload = {"BUTTONNAME": "REFRESH", "LOGINID": login_id}
        return await self._already_triggered_check("GetCollateralValuation", payload, segment="COLVAL")

    @otel_trace
    async def check_daily_margin_reporting_triggered(
        self, login_id: str, margin_date: date,
    ) -> AlreadyTriggeredResult:
        """
        POST {PROCESS_URL}/v1/api/process/CombinedMarginProcess with
        BUTTONNAME="REFRESH" and no MARGINDATE (confirmed against
        EDP_Trade_Process_API_v3 step 35). margin_date is accepted but
        unused, same reason as check_collateral_valuation_triggered().
        """
        payload = {"BUTTONNAME": "REFRESH", "LOGINID": login_id}
        return await self._already_triggered_check("CombinedMarginProcess", payload, segment="DMRPT")

    @otel_trace
    async def check_collateral_allocation_triggered(
        self, login_id: str, trade_date: date,
    ) -> AlreadyTriggeredResult:
        """Reuses file_process_status(MTFCOLLALLOC) — CBOS's own check
        endpoint for whether collateral allocation already ran today."""
        return await self._already_triggered_via_file_status("COLALLOC", "MTFCOLLALLOC", login_id, trade_date)

    @otel_trace
    async def check_mtf_fund_transfer_triggered(
        self, login_id: str, trade_date: date,
    ) -> AlreadyTriggeredResult:
        """Reuses file_process_status(MTFFUNDTRAN)."""
        return await self._already_triggered_via_file_status("MTFFT", "MTFFUNDTRAN", login_id, trade_date)

    @otel_trace
    async def check_daily_margin_statements_triggered(
        self, login_id: str, trade_date: date,
    ) -> AlreadyTriggeredResult:
        """Reuses file_process_status(CHECKDAILYMARGINSTATEMENT)."""
        return await self._already_triggered_via_file_status(
            "DMSTMT", "CHECKDAILYMARGINSTATEMENT", login_id, trade_date,
        )

    async def _already_triggered_via_file_status(
        self, segment: str, process_name: str, user_id: str, trade_date: date | None = None,
    ) -> AlreadyTriggeredResult:
        """
        Real mode: reuses file_process_status(process_name) but does NOT
        use FileStatusResult.is_ready — this ProcessName's MSG is a full
        sentence, not TRUE/FALSE/SKIP (see module docstring and
        _parse_already_triggered_sentence()); is_ready would always read
        False here, defeating the double-trigger guard.

        Mock mode: deliberately does not reuse file_process_status's own
        poll counter (keyed by (segment, process_name), it would
        independently "become ready" and falsely report "already
        triggered" on a plain happy-path run). Instead shares the same
        explicit opt-in mock as the REFRESH-based checks.
        """
        if self.use_mock:
            return self._mock_already_triggered(segment)
        # Shape B: these steps carry no Segment field in V5.
        result = await self.file_process_status(
            segment=segment, process_name=process_name, user_id=user_id,
            trade_date=trade_date, include_segment=False,
        )
        if result.is_error:
            return AlreadyTriggeredResult(
                already_triggered=False, raw_body=result.raw_body,
                error=result.error, is_transient=result.is_transient,
            )
        already_triggered = _parse_already_triggered_sentence(result.response)
        return AlreadyTriggeredResult(already_triggered=already_triggered, raw_body=result.raw_body)

    async def _already_triggered_check(
        self, endpoint_name: str, payload: dict, segment: str,
    ) -> AlreadyTriggeredResult:
        """Shared REFRESH-variant call — same Table1-non-empty-means-running
        response shape as getNewTradeProcess, reused here for the "already
        triggered" pre-checks that share an endpoint with a trigger call."""
        if self.use_mock:
            result = self._mock_already_triggered(segment)
            logger.info(
                f"[CBOS][MOCK] segment={segment} api={endpoint_name}(REFRESH) "
                f"| already_triggered={result.already_triggered}"
            )
            return result

        url = f"{self.process_url}/v1/api/process/{endpoint_name}"
        logger.info(f"[CBOS] segment={segment} api={endpoint_name}(REFRESH) | POST {url}")

        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                body = resp.text[:5000]
                if resp.status_code != 200:
                    logger.error(
                        f"[CBOS] segment={segment} api={endpoint_name}(REFRESH) "
                        f"| HTTP {resp.status_code} elapsed_ms={elapsed_ms}"
                    )
                    return AlreadyTriggeredResult(
                        already_triggered=False, raw_body=body,
                        error=f"HTTP {resp.status_code}", is_transient=_is_transient_http_status(resp.status_code),
                    )
                data = _json.loads(body)
                if data.get("Status") != "Success":
                    return AlreadyTriggeredResult(
                        already_triggered=False, raw_body=body,
                        error=f"CBOS Status={data.get('Status')}",
                    )
                table1 = data.get("Result", {}).get("Table1", [])
                already_triggered = bool(table1)
                logger.info(
                    f"[CBOS] segment={segment} api={endpoint_name}(REFRESH) "
                    f"| already_triggered={already_triggered} elapsed_ms={elapsed_ms}"
                )
                return AlreadyTriggeredResult(already_triggered=already_triggered, raw_body=body)
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            logger.error(
                f"[CBOS] segment={segment} api={endpoint_name}(REFRESH) "
                f"| EXCEPTION elapsed_ms={elapsed_ms} error={exc}"
            )
            return AlreadyTriggeredResult(already_triggered=False, error=str(exc), is_transient=True)

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
                        is_transient=_is_transient_http_status(resp.status_code),
                    )
                success, message, is_transient = _parse_post_trade_trigger(body)
                if not success:
                    logger.error(
                        f"[CBOS] segment={segment} api={endpoint_name} "
                        f"| CBOS rejected request message={message} elapsed_ms={elapsed_ms} "
                        f"is_transient={is_transient}"
                    )
                    return PostTradeTriggerResult(
                        success=False, message=message, raw_body=body, error=message,
                        is_transient=is_transient,
                    )
                logger.info(
                    f"[CBOS] segment={segment} api={endpoint_name} "
                    f"| message={message} elapsed_ms={elapsed_ms}"
                )
                return PostTradeTriggerResult(success=True, message=message, raw_body=body)
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            logger.error(
                f"[CBOS] segment={segment} api={endpoint_name} "
                f"| EXCEPTION elapsed_ms={elapsed_ms} error={exc}"
            )
            return PostTradeTriggerResult(success=False, error=str(exc), is_transient=True)

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

        process_id="0" (reserve): allocates the next incrementing fake PID,
        keyed by (segment, trade_date) so a later get_existing_process_id()
        call correctly reports "found" — mirrors real CBOS persistence.

        process_id=<actual> (trigger/recovery-check): the 1st call for a
        (segment, trade_date) returns an empty Table2 (nothing started
        yet); every call after returns one step IN_PROGRESS, so tests can
        exercise both branches of RealSegmentStateMachine.handle_triggered().
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
        """Simulates getdropdown EXISTINGPROCESSID.

        The UPLOADER is the sole PID reserver in the real pipeline (the
        agent only reads — see RealSegmentStateMachine's module docstring),
        so the mock plays the uploader's part: after
        _mock_uploader_reserve_delay misses for a (segment, trade_date),
        it provisions a PID as if the uploader had just reserved one, and
        reports "found" from then on. Delay 0 (default) provisions on the
        first lookup — the fastest happy path. Tests exercise the agent's
        waiting behaviour via mock_set_uploader_reserve_delay(n).

        A PID already present (a test pre-reserved it via
        get_new_trade_process(PROCESSID="0"), playing the uploader
        explicitly) is returned as-is, never re-minted."""
        key = (segment.upper(), trade_date.isoformat())
        pid = self._mock_reserved_pids.get(key)
        if not pid:
            lookups = self._mock_existing_pid_lookups.get(key, 0) + 1
            self._mock_existing_pid_lookups[key] = lookups
            if lookups <= self._mock_uploader_reserve_delay:
                return ExistingProcessResult(found=False, raw_body='{"Status":"Success","Result":[]}')
            pid = str(next(self._mock_pid_counter))
            self._mock_reserved_pids[key] = pid
        return ExistingProcessResult(
            found=True,
            process_id=pid,
            description=f"{pid} - CV0001 - Mock Entry",
        )

    def _mock_already_triggered(self, segment: str) -> AlreadyTriggeredResult:
        """Simulates a REFRESH-variant "already triggered" check — True only
        for segments explicitly marked via mock_mark_already_triggered()."""
        return AlreadyTriggeredResult(already_triggered=segment.upper() in self._mock_already_triggered_segments)

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

    def mock_set_uploader_reserve_delay(self, n: int) -> None:
        """Set how many getdropdown(EXISTINGPROCESSID) lookups miss before the
        mock provisions a PID as if the uploader had reserved it (default 0 —
        provisioned on the first lookup)."""
        self._mock_uploader_reserve_delay = n

    def mock_reset_counts(self) -> None:
        """Reset all poll counters and reserved PIDs (useful between test cases)."""
        self._mock_call_counts.clear()
        self._mock_reserved_pids.clear()
        self._mock_trigger_calls.clear()
        self._mock_already_triggered_segments.clear()
        self._mock_existing_pid_lookups.clear()

    def mock_mark_already_triggered(self, segment: str) -> None:
        """Opt a post-trade process into the "already triggered" branch —
        its next WAITING_FOR_GTG check takes the direct edge straight to
        WAITING_FOR_COMPLETION instead of moving to TRIGGERED."""
        self._mock_already_triggered_segments.add(segment.upper())


# =============================================================================
# Response parsers
# =============================================================================

def _is_transient_http_status(status_code: int) -> bool:
    """Whether a non-200 HTTP status from CBOS should be retried (BLOCKED)
    rather than treated as permanent. 5xx and 429 (rate limit) are both
    transient — misclassifying 429 as permanent would fail a segment the
    first time CBOS throttles it instead of just backing off."""
    return status_code >= 500 or status_code == 429


def _parse_msg(body: str) -> str:
    """
    Parse the MSG value from a file_process_status response.
    Expected: {"Status":"Success","Data":[{"MSG":"TRUE"}]}
    Falls back to string search if JSON parsing fails.

    A non-"Success" top-level Status means CBOS rejected the request; this
    must NOT be silently read as "not ready yet" (FALSE), or the poller
    would retry forever instead of surfacing the error.
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


def _parse_already_triggered_sentence(msg: str) -> bool:
    """
    Classify the MSG sentence returned by the 3 "already triggered" checks
    that share file_process_status but don't use TRUE/FALSE/SKIP
    (MTFCOLLALLOC / MTFFUNDTRAN / CHECKDAILYMARGINSTATEMENT — see module
    docstring). msg is already uppercased by _parse_msg().

    The two documented sentences ("PROCESS TRIGGERED IS PENDING",
    "DAILYMARGINSTATEMENT IS NOT TRIGGERED") mean not yet triggered. There's
    no documented sample of the already-triggered phrasing, so any other
    sentence is conservatively treated as already triggered — safer to
    skip a re-fire than risk a double-fire.

    isinstance guard is a defensive backstop (msg is always a str in
    practice) so a future type regression still degrades to "don't re-fire".
    """
    if not isinstance(msg, str):
        return True
    if msg == "TRUE":
        return True
    if msg == "FALSE" or "NOT TRIGGERED" in msg or "PENDING" in msg:
        return False
    return True


def _looks_like_html_error_page(body: str) -> bool:
    """Cheap guard for _parse_post_trade_trigger()'s malformed-but-200
    fallback: a misconfigured proxy can return HTTP 200 with an HTML error
    page instead of a real CBOS response. That must NOT fall into the
    "assume success" branch — reporting an outage as a completed
    post-trade trigger is a billing-correctness risk."""
    if not body:
        return False
    snippet = body.strip()[:300].lower()
    return snippet.startswith(("<html", "<!doctype")) or "<body" in snippet or "<head" in snippet


def _parse_post_trade_trigger(body: str) -> tuple[bool, str, bool]:
    """
    Parse a post-trade trigger response body. Expected shape:
      {"Status":"Success","Data":[{"MSG":"Process started successfully"}]}
    A non-"Success" top-level Status means CBOS rejected the request
    (permanent, is_transient=False).

    Falls back to treating any other unparsable-but-200 body as success
    (some endpoints, e.g. MTF Fund Transfer, have no guaranteed JSON shape)
    UNLESS it looks like an HTML error page (_looks_like_html_error_page),
    which is reported as transient so a proxy/LB outage gets retried
    instead of silently marking a real trigger as done.

    Returns (success, message, is_transient).
    """
    try:
        data = _json.loads(body)
        status = data.get("Status")
        if status and status != "Success":
            return False, f"CBOS Status={status}", False
        msg = None
        items = data.get("Data")
        if isinstance(items, list) and items:
            msg = items[0].get("MSG")
        if not msg:
            msg = data.get("MSG") or data.get("Message")
        return True, msg or "Process started successfully", False
    except Exception:
        if _looks_like_html_error_page(body):
            return (
                False,
                "Malformed 200 response looked like an HTML error page, not a CBOS acknowledgment",
                True,
            )
        return True, (body[:200] if body else "Process started successfully"), False


def _parse_new_trade_process(body: str) -> NewTradeProcessResult:
    """
    Parse the getNewTradeProcess response and extract PROCESSID + step list.

    Two failure modes, classified differently:
      - Status != "Success": a real, well-formed rejection — permanent
        (is_transient=False).
      - Malformed/unparsable 200 body (`except` below): more likely a
        transient CBOS glitch than a real rejection, so is_transient=True
        lets the next poll's well-formed response succeed instead of
        failing the segment outright.
    """
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
        return NewTradeProcessResult(success=False, raw_body=body, error=str(exc), is_transient=True)
