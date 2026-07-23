"""V6 Step-10 gate (wayfinder ticket 15 / EDP_Trade_Process_API_Documentation_V6):
after FILEUPLOAD goes TRUE the engine must poll
file_process_status(CHECKINSTITRADE) and hold in WAITING_FOR_INSTI_TRADE
until Institutional Trade Transfer confirms complete — only then may the
trade-process trigger fire. CBOS does NOT enforce this server-side (the V6
doc warns early triggers "may cause pipeline step failures"), so these
tests pin the engine as the enforcement point:

  - the happy path traverses WAITING_FOR_INSTI_TRADE (state + audit JSON),
  - the trigger NEVER fires while CHECKINSTITRADE answers FALSE,
  - ANY non-TRUE answer (SKIP, values outside the V6 FALSE/TRUE vocabulary)
    holds the gate closed rather than advancing OR failing — ticket 15 and
    CBOS_HANDOFF_CONTRACT both fix the posture as "non-TRUE = wait", with
    the segment window as the timeout backstop.
"""

from __future__ import annotations

from datetime import date

from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient, FileStatusResult, NewTradeProcessResult

from . import helpers
from .fakes import SkippingCbosClient

SEGMENT = "EQ"


class InstiTradeNeverReadyCbosClient(CbosClient):
    """Normal in-process mock EXCEPT CHECKINSTITRADE always answers FALSE for
    one segment — Insti Trade Transfer that never completes. Also counts
    trigger-mode getNewTradeProcess calls, to prove the trigger is never
    fired past a closed gate."""

    def __init__(self, status_url: str, process_url: str, *, segment: str):
        super().__init__(status_url, process_url, use_mock=True)
        self._segment = segment.upper()
        self.trigger_calls = 0

    async def file_process_status(
        self, segment: str, process_name: str, user_id: str, trade_date=None, *, include_segment=True,
    ) -> FileStatusResult:
        if segment.upper() == self._segment and process_name == "CHECKINSTITRADE":
            return FileStatusResult(
                response="FALSE",
                raw_body='{"Status":"Success","Data":[{"MSG":"FALSE"}]}',
            )
        return await super().file_process_status(segment, process_name, user_id, trade_date, include_segment=include_segment)

    async def get_new_trade_process(
        self, group_name: str, login_id: str, trade_date: date, process_id: str = "0",
    ) -> NewTradeProcessResult:
        # Only the gated segment's triggers matter — the other 8 segments
        # legitimately proceed and fire theirs.
        if group_name.upper() == self._segment and process_id != "0":
            self.trigger_calls += 1
        return await super().get_new_trade_process(group_name, login_id, trade_date, process_id)


class _FixedAnswerCbosClient(CbosClient):
    """Normal in-process mock EXCEPT one (segment, process) pair always gets
    a fixed literal answer — used to probe out-of-vocabulary responses."""

    def __init__(self, status_url: str, process_url: str, *, segment: str, process: str, answer: str):
        super().__init__(status_url, process_url, use_mock=True)
        self._segment, self._process, self._answer = segment.upper(), process, answer

    async def file_process_status(
        self, segment: str, process_name: str, user_id: str, trade_date=None, *, include_segment=True,
    ) -> FileStatusResult:
        if segment.upper() == self._segment and process_name == self._process:
            return FileStatusResult(
                response=self._answer,
                raw_body=f'{{"Status":"Success","Data":[{{"MSG":"{self._answer}"}}]}}',
            )
        return await super().file_process_status(segment, process_name, user_id, trade_date, include_segment=include_segment)


async def test_happy_path_traverses_insti_trade_gate(cfg, session_factory, test_date):
    """The full pipeline records a WAITING_FOR_INSTI_TRADE stage with a
    CHECKINSTITRADE_STATUS step that finished ready — the gate is IN the
    path, not skippable (the transition map has no direct
    WAITING_FOR_FILE_UPLOAD -> TRIGGERED edge any more)."""
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    row = {r.segment_code: r for r in rows}[SEGMENT]

    assert row.segment_status == SegmentStatus.COMPLETED
    stage = row.processes_json[SegmentState.WAITING_FOR_INSTI_TRADE.value]
    assert stage["status"] == "COMPLETED"
    step = stage["steps"]["CHECKINSTITRADE_STATUS"]
    assert step["last_response"] == "TRUE"
    assert "ready_at" in step, "gate completion must stamp ready_at (a GTG, not a confirmation)"


async def test_trigger_never_fires_while_insti_trade_false(cfg, session_factory, test_date):
    """CHECKINSTITRADE stuck on FALSE: the segment parks in
    WAITING_FOR_INSTI_TRADE cycle after cycle and the trigger-mode
    getNewTradeProcess call count stays ZERO — the exact premature trigger
    the V6 doc warns about can never be issued."""
    cbos = InstiTradeNeverReadyCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, segment=SEGMENT,
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    for _ in range(6):  # plenty of cycles to reach and then sit at the gate
        await helpers.run_one_cycle(orchestrator, session_factory, test_date)

    row = {r.segment_code: r for r in await helpers.get_rows(session_factory, test_date)}[SEGMENT]
    assert row.current_state == SegmentState.WAITING_FOR_INSTI_TRADE
    assert row.segment_status not in (SegmentStatus.COMPLETED, SegmentStatus.FAILED)
    assert cbos.trigger_calls == 0, (
        "trigger-mode getNewTradeProcess must never fire while Insti Trade "
        "Transfer is incomplete"
    )
    # And the audit trail shows the poll happening (waiting, not wedged).
    step = row.processes_json[SegmentState.WAITING_FOR_INSTI_TRADE.value]["steps"]["CHECKINSTITRADE_STATUS"]
    assert step["last_response"] == "FALSE"


async def test_any_non_true_answer_holds_the_gate(cfg, session_factory, test_date):
    """Review finding: the generic _poll_confirmation helper ADVANCES on any
    answer that isn't TRUE/FALSE/SKIP — at this gate that would fire the
    premature trigger V6 explicitly warns about (MSG="HOLIDAY" would have
    triggered billing). The gate handler must treat every non-TRUE answer —
    SKIP and out-of-vocabulary values alike — as "hold and wait", never
    advance and never fail: ticket 15 and CBOS_HANDOFF_CONTRACT both pin
    the posture as non-TRUE = wait (window timeout backstops)."""
    for weird_answer_client in (
        SkippingCbosClient(  # answers SKIP for CHECKINSTITRADE
            cfg.cbos_status_url, cfg.cbos_process_url,
            skip_segment=SEGMENT, skip_process="CHECKINSTITRADE",
        ),
        _FixedAnswerCbosClient(  # answers an out-of-vocabulary value
            cfg.cbos_status_url, cfg.cbos_process_url,
            segment=SEGMENT, process="CHECKINSTITRADE", answer="HOLIDAY",
        ),
    ):
        weird_answer_client.mock_set_ready_after(1)
        orchestrator = EdpOrchestrator(cfg, weird_answer_client)

        await helpers.cleanup_day(session_factory, test_date)
        await helpers.seed_day(session_factory, test_date, cfg)
        for _ in range(6):
            await helpers.run_one_cycle(orchestrator, session_factory, test_date)

        row = {r.segment_code: r for r in await helpers.get_rows(session_factory, test_date)}[SEGMENT]
        assert row.current_state == SegmentState.WAITING_FOR_INSTI_TRADE, (
            f"non-TRUE answer must hold the gate, got state={row.current_state}"
        )
        assert row.segment_status not in (SegmentStatus.COMPLETED, SegmentStatus.FAILED), (
            "non-TRUE must neither advance (premature trigger) nor fail (contract says wait)"
        )
