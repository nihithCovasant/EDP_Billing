"""
In-memory state for the Mock CBOS server.

Simulates the stateful behaviour real CBOS exhibits across the polling
sequence (BeginFileUpload -> FILEUPLOAD -> BILLPOSTING -> RECON ->
CONTRACTNOTEGENERATION), identical for all 9 segments (CASH/EQ, F&O/DR,
CD/CUR, SLBM/SL, MCX, MCXPHY, NCDEX, NCDEXPHY, MTF), plus the 5 T+1 post-trade processes
(COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT — GTG/confirm polling reuses the
same file_status() method as the segments, keyed by (process_code,
gtg_process_name) where gtg_process_name comes from the agent's uploaded
workflow config or falls back to mock_cbos.constants.DEFAULT_GTG_PROCESS_NAME),
without any external dependency — pure Python dict, reset on server restart
or via the /mock/reset control endpoint.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .constants import resolve_gtg_process_name


@dataclass
class MockCbosState:
    # How many polls a (segment, process_name) pair needs before returning TRUE
    ready_after: int = 2

    # (segment, process_name) -> poll count so far
    poll_counts: Dict[Tuple[str, str], int] = field(default_factory=dict)

    # (group_name, trade_date) -> reserved PROCESSID
    reserved_pids: Dict[Tuple[str, str], str] = field(default_factory=dict)

    # PROCESSID -> True once getNewTradeProcess was called with the real PID (trigger fired)
    executed_pids: Dict[str, bool] = field(default_factory=dict)

    # Segments for which BeginFileUpload should always return SKIP (holiday simulation)
    holiday_segments: set = field(default_factory=set)

    # (segment, process_name) pairs pinned to always return FALSE (stuck / timeout simulation)
    stuck_keys: set = field(default_factory=set)

    # (segment, process_name) pairs pinned to always return TRUE immediately
    force_ready_keys: set = field(default_factory=set)

    # Post-trade processes that have had their trigger endpoint called at
    # least once — keyed by process_code (COLVAL, ...). Values hold the
    # LOGINID that was on the trigger payload (config-driven login_id from
    # workflow_json.post_trade_processes[].login_id) plus a timestamp for
    # /mock/state debugging. GTG/confirm poll behaviour is still driven by
    # poll_counts above, keyed by (process_code, gtg_process_name).
    post_trade_triggered: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Last few file_process_status calls — useful for verifying the agent
    # sent the config-resolved (Segment, ProcessName, UserID) triple.
    recent_file_status_calls: List[Dict[str, str]] = field(default_factory=list)
    _max_recent_calls: int = 20

    _pid_counter: itertools.count = field(default_factory=lambda: itertools.count(17001))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -------------------------------------------------------------------------
    # file_process_status (Good-to-Go polling)
    # -------------------------------------------------------------------------

    def file_status(self, segment: str, process_name: str, user_id: str = "") -> str:
        """Return TRUE | FALSE | SKIP for the given (segment, process_name)."""
        with self._lock:
            seg = segment.upper()
            if process_name == "BeginFileUpload" and seg in self.holiday_segments:
                return "SKIP"

            key = (seg, process_name)

            self._record_file_status_call(seg, process_name, user_id)

            if key in self.stuck_keys:
                return "FALSE"

            if key in self.force_ready_keys:
                return "TRUE"

            self.poll_counts[key] = self.poll_counts.get(key, 0) + 1
            if self.poll_counts[key] >= self.ready_after:
                return "TRUE"
            return "FALSE"

    def _record_file_status_call(self, segment: str, process_name: str, user_id: str) -> None:
        entry = {
            "segment": segment,
            "process_name": process_name,
            "user_id": user_id or "",
        }
        self.recent_file_status_calls.append(entry)
        if len(self.recent_file_status_calls) > self._max_recent_calls:
            self.recent_file_status_calls = self.recent_file_status_calls[-self._max_recent_calls:]

    # -------------------------------------------------------------------------
    # getNewTradeProcess (reserve + trigger)
    # -------------------------------------------------------------------------

    def reserve_process_id(self, group_name: str, trade_date: str) -> str:
        with self._lock:
            key = (group_name.upper(), trade_date)
            if key not in self.reserved_pids:
                self.reserved_pids[key] = str(next(self._pid_counter))
            return self.reserved_pids[key]

    def mark_executed(self, process_id: str) -> None:
        with self._lock:
            self.executed_pids[str(process_id)] = True

    def is_executed(self, process_id: str) -> bool:
        return self.executed_pids.get(str(process_id), False)

    def find_reserved_pid(self, group_name: str, trade_date: str) -> Optional[str]:
        return self.reserved_pids.get((group_name.upper(), trade_date))

    # -------------------------------------------------------------------------
    # Post-trade (T+1) triggers — Collateral Valuation/Allocation, MTF Fund
    # Transfer, Daily Margin Reporting/Statements
    # -------------------------------------------------------------------------

    def mark_post_trade_triggered(self, process_code: str, login_id: str = "") -> None:
        with self._lock:
            code = process_code.upper()
            self.post_trade_triggered[code] = {
                "login_id": login_id or "",
                "triggered_at": datetime.now().isoformat(timespec="seconds"),
            }

    def is_post_trade_triggered(self, process_code: str) -> bool:
        return process_code.upper() in self.post_trade_triggered

    def set_post_trade_stuck(
        self, process_code: str, enabled: bool, gtg_process_name: str | None = None,
    ) -> str:
        """
        Pin a post-trade GTG/confirm poll to always return FALSE.
        Returns the resolved gtg_process_name used as the stuck key.
        """
        code = process_code.upper()
        proc_name = resolve_gtg_process_name(code, gtg_process_name)
        self.set_stuck(code, proc_name, enabled)
        return proc_name

    def set_post_trade_force_ready(
        self, process_code: str, enabled: bool, gtg_process_name: str | None = None,
    ) -> str:
        """
        Pin a post-trade GTG/confirm poll to always return TRUE immediately.
        Returns the resolved gtg_process_name used as the force_ready key.
        """
        code = process_code.upper()
        proc_name = resolve_gtg_process_name(code, gtg_process_name)
        self.set_force_ready(code, proc_name, enabled)
        return proc_name

    # -------------------------------------------------------------------------
    # Admin / control helpers (used by /mock/* endpoints)
    # -------------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self.poll_counts.clear()
            self.reserved_pids.clear()
            self.executed_pids.clear()
            self.holiday_segments.clear()
            self.stuck_keys.clear()
            self.force_ready_keys.clear()
            self.post_trade_triggered.clear()
            self.recent_file_status_calls.clear()
            self._pid_counter = itertools.count(17001)

    def set_ready_after(self, n: int) -> None:
        with self._lock:
            self.ready_after = max(1, n)

    def set_holiday(self, segment: str, enabled: bool) -> None:
        with self._lock:
            seg = segment.upper()
            if enabled:
                self.holiday_segments.add(seg)
            else:
                self.holiday_segments.discard(seg)

    def set_stuck(self, segment: str, process_name: str, enabled: bool) -> None:
        with self._lock:
            key = (segment.upper(), process_name)
            if enabled:
                self.stuck_keys.add(key)
            else:
                self.stuck_keys.discard(key)

    def set_force_ready(self, segment: str, process_name: str, enabled: bool) -> None:
        with self._lock:
            key = (segment.upper(), process_name)
            if enabled:
                self.force_ready_keys.add(key)
            else:
                self.force_ready_keys.discard(key)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ready_after": self.ready_after,
                "poll_counts": {f"{k[0]}::{k[1]}": v for k, v in self.poll_counts.items()},
                "reserved_pids": {f"{k[0]}::{k[1]}": v for k, v in self.reserved_pids.items()},
                "executed_pids": dict(self.executed_pids),
                "holiday_segments": sorted(self.holiday_segments),
                "stuck_keys": [f"{k[0]}::{k[1]}" for k in self.stuck_keys],
                "force_ready_keys": [f"{k[0]}::{k[1]}" for k in self.force_ready_keys],
                "post_trade_triggered": dict(self.post_trade_triggered),
                "recent_file_status_calls": list(self.recent_file_status_calls),
            }


# Single process-wide instance — the mock server is meant to be run standalone,
# one process per test session.
state = MockCbosState()


# ---------------------------------------------------------------------------
# Static reference data — the 28 upload steps from the API doc (Step 2 Table2)
# ---------------------------------------------------------------------------

UPLOAD_STEP_NAMES: List[Tuple[int, str, int]] = [
    (1, "Settlement Master NSE Upload", 551),
    (2, "Settlement Master BSE Upload", 678),
    (3, "BSE Scrip Upload", 81),
    (4, "NSE Scrip Upload", 82),
    (5, "NSE BSE InterOperable Scrip Mapping", 83),
    (6, "STT Indicator Upload", 84),
    (7, "STT not to Charge Upload", 94),
    (8, "BSE UDIFF Trade File Upload", 546),
    (9, "BSE Trade File Upload", 85),
    (10, "NSE Trade File Upload", 545),
    (11, "NSE Notice Trade File Upload", 86),
    (12, "BSE AUCTION Trade File Upload", 451),
]
# Steps 13-28: auto-run (Trade Merger / Charges / Bill Posting / STK), upload_id=0
UPLOAD_STEP_NAMES += [
    (n, "Trade Merger / Charges / Bill Posting / STK (auto-run)", 0) for n in range(13, 29)
]


def build_table2(all_success: bool) -> List[dict]:
    """Build the Table2 step list for getNewTradeProcess responses."""
    status = "SUCCESS" if all_success else "PENDING"
    return [
        {
            "STEPNO": step_no,
            "NAME": name,
            "STATUS": status,
            "UPLOADID": upload_id,
        }
        for step_no, name, upload_id in UPLOAD_STEP_NAMES
    ]
