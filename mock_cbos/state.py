"""
In-memory state for the Mock CBOS server.

Simulates the stateful behaviour real CBOS exhibits across the polling
sequence (BeginFileUpload -> FILEUPLOAD -> BILLPOSTING -> RECON ->
CONTRACTNOTEGENERATION), identical for all 7 segments (CASH/EQ, F&O/DR,
CD/CUR, SLBM/SL, MCX, NCDEX, MTF), plus the 5 T+1 post-trade processes
(COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT — GTG/confirm polling reuses the
same file_status() method as the segments, keyed by their own ProcessName),
without any external dependency — pure Python dict, reset on server restart
or via the /mock/reset control endpoint.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


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

    # Post-trade processes (COLVAL/COLALLOC/MTFFT/DMRPT/DMSTMT) that have
    # had their trigger endpoint called at least once — purely informational
    # (used by /mock/state for debugging), the file_status() poll counters
    # above are what actually drive GTG/confirm poll behaviour.
    post_trade_triggered: set = field(default_factory=set)

    _pid_counter: itertools.count = field(default_factory=lambda: itertools.count(17001))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -------------------------------------------------------------------------
    # file_process_status (Good-to-Go polling)
    # -------------------------------------------------------------------------

    def file_status(self, segment: str, process_name: str) -> str:
        """Return TRUE | FALSE | SKIP for the given (segment, process_name)."""
        with self._lock:
            if process_name == "BeginFileUpload" and segment.upper() in self.holiday_segments:
                return "SKIP"

            key = (segment.upper(), process_name)

            if key in self.stuck_keys:
                return "FALSE"

            if key in self.force_ready_keys:
                return "TRUE"

            self.poll_counts[key] = self.poll_counts.get(key, 0) + 1
            if self.poll_counts[key] >= self.ready_after:
                return "TRUE"
            return "FALSE"

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

    def mark_post_trade_triggered(self, segment: str) -> None:
        with self._lock:
            self.post_trade_triggered.add(segment.upper())

    def is_post_trade_triggered(self, segment: str) -> bool:
        return segment.upper() in self.post_trade_triggered

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
                "post_trade_triggered": sorted(self.post_trade_triggered),
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
