"""
Fake / controllable CBOS clients used by the test suite.

These build on top of src.tools.cbos_client.CbosClient's own in-process mock
(use_mock=True) instead of reimplementing CBOS response shapes from scratch,
so tests stay in sync with the real client's mock behaviour automatically.
"""

from __future__ import annotations

from datetime import date

from src.tools.cbos_client import CbosClient, FileStatusResult, NewTradeProcessResult


class FailingCbosClient(CbosClient):
    """
    Behaves exactly like CbosClient(use_mock=True) for every call EXCEPT
    one (segment, process_name) pair, which always returns a permanent
    (non-transient) CBOS error — used to simulate a segment failing partway
    through its 7-stage pipeline (e.g. BILLPOSTING, the 2nd process after
    fileupload, per the process "order" convention documented in
    models.EdpProperties).

    A permanent (is_transient=False) error is what makes the pipeline call
    pipeline.stages._fail() and mark the segment FAILED — a transient error
    would just retry forever (StageResult.BLOCKED).
    """

    def __init__(self, status_url: str, process_url: str, *, fail_segment: str, fail_process: str):
        super().__init__(status_url, process_url, use_mock=True)
        self._fail_segment = fail_segment.upper()
        self._fail_process = fail_process

    async def file_process_status(
        self, segment: str, process_name: str, user_id: str
    ) -> FileStatusResult:
        if segment.upper() == self._fail_segment and process_name == self._fail_process:
            return FileStatusResult(
                response="FALSE",
                raw_body='{"Status":"CBOS_INTERNAL_ERROR"}',
                error=f"Simulated permanent CBOS failure for {segment}/{process_name}",
                is_transient=False,
            )
        return await super().file_process_status(segment, process_name, user_id)


class TransientTriggerFailureCbosClient(CbosClient):
    """
    Fails the FIRST trigger-mode getNewTradeProcess call (PROCESSID != "0")
    for one specific segment with a transient (network-like) error — every
    other call (including retries and the recovery check) behaves exactly
    like the normal in-process mock.

    Used to prove pipeline.stages.handle_trigger() leaves
    processes_json["trigger"]["status"] == "TRIGGERING" on a transient
    trigger-call error (never downgrading it to "FAILED", and never
    treating the row itself as failed) — so the very next wake cycle
    correctly re-enters the recovery decision tree instead of assuming the
    call was never sent, or worse, blindly re-sending it without checking.
    """

    def __init__(self, status_url: str, process_url: str, *, fail_segment: str):
        super().__init__(status_url, process_url, use_mock=True)
        self._fail_segment = fail_segment.upper()
        self._fired = False

    async def get_new_trade_process(
        self, group_name: str, login_id: str, trade_date: date, process_id: str = "0",
    ) -> NewTradeProcessResult:
        if not self._fired and group_name.upper() == self._fail_segment and process_id != "0":
            self._fired = True
            return NewTradeProcessResult(
                success=False,
                error="Simulated transient network error on trigger call",
                is_transient=True,
            )
        return await super().get_new_trade_process(group_name, login_id, trade_date, process_id)
