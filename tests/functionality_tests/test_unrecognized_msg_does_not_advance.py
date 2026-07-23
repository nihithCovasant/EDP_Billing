"""
Regression coverage: a file_process_status MSG that is none of the
documented TRUE/FALSE/SKIP values (garbled response, unexpected sentence,
whitespace) must NOT be read as "ready" — the state machine's fallthrough
used to assume anything that wasn't an explicit error/SKIP/FALSE meant
"proceed," which would advance the pipeline on a response CBOS never
actually confirmed as ready. It must instead be treated as not-yet-ready
(BLOCKED, retried next cycle), the same as an ordinary "FALSE" poll.
"""

from __future__ import annotations

from datetime import date

from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient, FileStatusResult

from .. import helpers


class GarbledResponseCbosClient(CbosClient):
    """Returns an unrecognized (non TRUE/FALSE/SKIP) MSG for one specific
    (segment, process_name) pair; behaves like the normal in-process mock
    for everything else."""

    def __init__(self, status_url: str, process_url: str, *, garble_segment: str, garble_process: str):
        super().__init__(status_url, process_url, use_mock=True)
        self._garble_segment = garble_segment.upper()
        self._garble_process = garble_process

    async def file_process_status(
        self, segment: str, process_name: str, user_id: str, trade_date: date, include_segment: bool = True,
    ) -> FileStatusResult:
        if segment.upper() == self._garble_segment and process_name == self._garble_process:
            return FileStatusResult(response="GARBLED_UNEXPECTED_VALUE", raw_body="garbage", error=None)
        return await super().file_process_status(segment, process_name, user_id, trade_date, include_segment)


async def test_unrecognized_init_response_blocks_instead_of_advancing(cfg, session_factory, test_date):
    """EQ's INIT holiday check (BeginFileUpload) returns an unrecognized
    value — the segment must stay BLOCKED (IN_PROGRESS, still in INIT),
    never silently advance to WAITING_FOR_FILE_UPLOAD."""
    cbos = GarbledResponseCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, garble_segment="EQ", garble_process="BeginFileUpload",
    )
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    result = await helpers.run_one_cycle(orchestrator, session_factory, test_date)
    assert result["processed"] > 0

    rows = await helpers.get_rows(session_factory, test_date)
    eq_row = next(r for r in rows if r.segment_code == "EQ")
    assert eq_row.segment_status == SegmentStatus.IN_PROGRESS
    assert eq_row.current_state == SegmentState.INIT.value, (
        "an unrecognized BeginFileUpload response must not advance past INIT"
    )


async def test_unrecognized_post_trade_gtg_response_blocks_instead_of_advancing(cfg, session_factory, test_date):
    """COLVAL's WAITING_FOR_GTG poll returns an unrecognized value — must
    stay blocked in WAITING_FOR_GTG, never silently proceed to the
    already-triggered check / TRIGGERED."""
    cbos = GarbledResponseCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, garble_segment="COLVAL", garble_process="CollateralValuation",
    )
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_post_trade_day(session_factory, test_date)
    orchestrator._cycle_active_date = test_date
    orchestrator._cycle_now = helpers.fixed_post_trade_now_for(test_date, orchestrator._tz)

    outcome = await orchestrator._process_one_post_trade("COLVAL")
    assert outcome == "blocked"

    rows = await helpers.get_post_trade_rows(session_factory, test_date)
    colval_row = next(r for r in rows if r.segment_code == "COLVAL")
    assert colval_row.current_state == SegmentState.WAITING_FOR_GTG.value
