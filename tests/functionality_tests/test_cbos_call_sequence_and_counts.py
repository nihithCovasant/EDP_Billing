"""
CBOS call-sequence and call-count audit — end-to-end functionality tests.

CBOS is a real external system: every wasted/extra/duplicate call has a
cost, and every missing call could mean an incomplete pipeline. These tests
verify the EXACT SEQUENCE and EXACT COUNT of CBOS API calls made across a
full segment/post-trade-process lifecycle, using the real EdpOrchestrator +
real DB (tests/helpers.py conventions), same as the rest of the suite.

Per src/tools/cbos_client.py's module docstring, the segment pipeline is
always these 7 CBOS-touching steps:
  1. file_process_status(BeginFileUpload)        -> holiday check (INIT)
  2. getdropdown(EXISTINGPROCESSID); if not found,
     get_new_trade_process(PROCESSID="0")        -> reserve (UPLOADER-only call;
                                                    the agent must never fire it)
  3. file_process_status(FILEUPLOAD)             -> poll (WAITING_FOR_FILE_UPLOAD)
  4. get_new_trade_process(PROCESSID=<actual>)   -> trigger (TRIGGERED)
  5. file_process_status(BILLPOSTING)            -> poll (WAITING_FOR_BILLPOSTING)
  6. file_process_status(RECON)                  -> poll (WAITING_FOR_RECON)
  7. file_process_status(CONTRACTNOTEGENERATION) -> poll (WAITING_FOR_CONTRACT_NOTE_GENERATION)

and the post-trade pipeline (PostTradeStateMachine) is always:
  WAITING_FOR_GTG (poll, doubles as holiday check) -> "already triggered"
  check -> [TRIGGERED (trigger call) ->] WAITING_FOR_COMPLETION (poll)
"""

from __future__ import annotations

from datetime import date

from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.agent.edp.utils.constants import POST_TRADE_ORDER, SEGMENT_ORDER
from src.tools.cbos_client import (
    CbosClient,
    ExistingProcessResult,
    FileStatusResult,
    NewTradeProcessResult,
)

from .. import helpers
from ..fakes import SkippingCbosClient

# =============================================================================
# Call-sequence-recording fakes specific to this file.
# =============================================================================


class SequenceRecordingCbosClient(CbosClient):
    """
    Records EVERY distinct CBOS API call — file_process_status,
    get_new_trade_process (both reserve and trigger modes, tagged
    separately), and get_existing_process_id — as an ordered list of
    (segment, api_tag) tuples, in the exact order the state machine issues
    them. Extends fakes.RecordingFileStatusCbosClient's pattern (which only
    records file_process_status) to cover every CBOS method that could be
    called during the segment pipeline, so call ORDER (not just count) can
    be asserted precisely.

    api_tag values:
      "BeginFileUpload" / "FILEUPLOAD" / "BILLPOSTING" / "RECON" /
      "CONTRACTNOTEGENERATION"  -> file_process_status, tagged by process_name
      "getExistingProcessId"                -> get_existing_process_id
      "getNewTradeProcess:reserve"          -> get_new_trade_process(PROCESSID="0")
      "getNewTradeProcess:trigger"          -> get_new_trade_process(PROCESSID=<actual>)
    """

    def __init__(self, status_url: str, process_url: str):
        super().__init__(status_url, process_url, use_mock=True)
        self.calls: list[tuple[str, str]] = []

    async def file_process_status(
        self, segment: str, process_name: str, user_id: str
    ) -> FileStatusResult:
        self.calls.append((segment.upper(), process_name))
        return await super().file_process_status(segment, process_name, user_id)

    async def get_existing_process_id(
        self, segment: str, login_id: str, trade_date: date,
    ) -> ExistingProcessResult:
        self.calls.append((segment.upper(), "getExistingProcessId"))
        return await super().get_existing_process_id(segment, login_id, trade_date)

    async def get_new_trade_process(
        self, group_name: str, login_id: str, trade_date: date, process_id: str = "0",
    ) -> NewTradeProcessResult:
        mode = "reserve" if process_id == "0" else "trigger"
        self.calls.append((group_name.upper(), f"getNewTradeProcess:{mode}"))
        return await super().get_new_trade_process(group_name, login_id, trade_date, process_id)

    async def _post_trade_trigger(self, endpoint_name: str, payload: dict, segment: str):
        self.calls.append((segment.upper(), f"trigger:{endpoint_name}"))
        return await super()._post_trade_trigger(endpoint_name, payload, segment=segment)

    async def _already_triggered_check(self, endpoint_name: str, payload: dict, segment: str):
        self.calls.append((segment.upper(), f"alreadyTriggeredCheck:{endpoint_name}"))
        return await super()._already_triggered_check(endpoint_name, payload, segment=segment)

    async def _already_triggered_via_file_status(self, segment: str, process_name: str, user_id: str):
        self.calls.append((segment.upper(), f"alreadyTriggeredCheck:{process_name}"))
        return await super()._already_triggered_via_file_status(segment, process_name, user_id)

    def calls_for(self, segment: str) -> list[str]:
        seg = segment.upper()
        return [tag for (s, tag) in self.calls if s == seg]


class ExistingPidSequenceCbosClient(SequenceRecordingCbosClient):
    """
    Like SequenceRecordingCbosClient, but get_existing_process_id always
    reports a pre-existing PID for ONE specific segment — simulating RPA (or
    an earlier agent cycle) having already reserved a process_id via
    CBOS's getdropdown(EXISTINGPROCESSID) before this segment-day's pipeline
    ever runs. Used to prove the "existing PID" code path
    (RealSegmentStateMachine._resolve_process_id's `if existing.found`
    branch) never ALSO fires the reserve-mode getNewTradeProcess call —
    the two "get a process_id" paths are mutually exclusive per
    segment-day.
    """

    def __init__(self, status_url: str, process_url: str, *, existing_pid_segment: str, existing_pid: str = "99999"):
        super().__init__(status_url, process_url)
        self._existing_pid_segment = existing_pid_segment.upper()
        self._existing_pid = existing_pid

    async def get_existing_process_id(
        self, segment: str, login_id: str, trade_date: date,
    ) -> ExistingProcessResult:
        self.calls.append((segment.upper(), "getExistingProcessId"))
        if segment.upper() == self._existing_pid_segment:
            return ExistingProcessResult(
                found=True, process_id=self._existing_pid, description="pre-existing PID (RPA reserved)",
            )
        # Fall through to the real (non-recording) mock logic for every
        # other segment -- call CbosClient's implementation directly,
        # bypassing our own override to avoid double-recording this call.
        return await CbosClient.get_existing_process_id(self, segment, login_id, trade_date)


# The exact CBOS call sequence a full-pipeline segment must produce on the
# fastest possible happy path (mock_set_ready_after(1) -- every poll ready
# on its first try). Mirrors cbos_client.py's module-docstring step list.
EXPECTED_FULL_PIPELINE_SEQUENCE = [
    "BeginFileUpload",              # INIT holiday check
    "getExistingProcessId",         # WAITING_FOR_FILE_UPLOAD PID read (uploader is the
                                    # sole reserver; the mock's uploader-sim provisions
                                    # the PID on this first lookup, delay 0)
    "FILEUPLOAD",                   # WAITING_FOR_FILE_UPLOAD poll
    "getNewTradeProcess:trigger",   # TRIGGERED
    "BILLPOSTING",                  # WAITING_FOR_BILLPOSTING poll
    "RECON",                        # WAITING_FOR_RECON poll
    "CONTRACTNOTEGENERATION",       # WAITING_FOR_CONTRACT_NOTE_GENERATION poll
]


# =============================================================================
# Scenario 1 — A holiday-skipped segment makes EXACTLY ONE CBOS call, ever.
# =============================================================================

async def test_holiday_skipped_segment_makes_exactly_one_cbos_call_ever(cfg, session_factory, test_date):
    """
    EQ's INIT holiday check (BeginFileUpload) returns SKIP -> the segment
    goes straight to SKIPPED without ever entering WAITING_FOR_FILE_UPLOAD/
    TRIGGERED/etc. Confirm the segment's ENTIRE lifecycle produced exactly
    1 recorded CBOS call total -- no wasted calls to FILEUPLOAD/
    BILLPOSTING/RECON/CONTRACTNOTEGENERATION/getNewTradeProcess/
    getExistingProcessId ever happen for a segment that never even started.
    """

    class RecordingSkippingCbosClient(SequenceRecordingCbosClient):
        def __init__(self, status_url: str, process_url: str, *, skip_segment: str, skip_process: str):
            super().__init__(status_url, process_url)
            self._skip_segment = skip_segment.upper()
            self._skip_process = skip_process

        async def file_process_status(self, segment: str, process_name: str, user_id: str) -> FileStatusResult:
            self.calls.append((segment.upper(), process_name))
            if segment.upper() == self._skip_segment and process_name == self._skip_process:
                return FileStatusResult(response="SKIP", raw_body='{"Status":"SKIP"}', error=None, is_transient=False)
            return await CbosClient.file_process_status(self, segment, process_name, user_id)

    cbos = RecordingSkippingCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, skip_segment="EQ", skip_process="BeginFileUpload",
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    eq_row = by_code["EQ"]
    assert eq_row.segment_status == SegmentStatus.SKIPPED
    assert eq_row.skip_category == "CBOS_SKIP"

    eq_calls = cbos.calls_for("EQ")
    assert eq_calls == ["BeginFileUpload"], (
        f"EQ (holiday-skipped) must make EXACTLY ONE CBOS call ever (the INIT "
        f"BeginFileUpload holiday check), got {eq_calls}"
    )
    assert len(eq_calls) == 1


# =============================================================================
# Scenario 2 — Full-pipeline segment: exact ordered sequence, minimum polls.
# =============================================================================

async def test_full_pipeline_segment_exact_call_sequence_minimum_polls(cfg, session_factory, test_date):
    """
    A single real segment (EQ) running the full happy path with
    mock_set_ready_after(1) (every poll ready first try -- the fastest
    possible happy path) must produce EXACTLY the ordered CBOS call
    sequence documented in cbos_client.py's module docstring, with no
    extras and no gaps: BeginFileUpload -> getExistingProcessId ->
    FILEUPLOAD -> getNewTradeProcess(trigger) -> BILLPOSTING -> RECON ->
    CONTRACTNOTEGENERATION. No reserve-mode call anywhere: the uploader is
    the sole PID reserver, the agent only reads.
    """
    cbos = SequenceRecordingCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert by_code["EQ"].segment_status == SegmentStatus.COMPLETED

    eq_calls = cbos.calls_for("EQ")
    assert eq_calls == EXPECTED_FULL_PIPELINE_SEQUENCE, (
        f"EQ's exact CBOS call sequence does not match the expected happy-path "
        f"sequence.\nExpected: {EXPECTED_FULL_PIPELINE_SEQUENCE}\nActual:   {eq_calls}"
    )
    assert "getNewTradeProcess:reserve" not in eq_calls, (
        "the agent must NEVER fire the reserve-mode getNewTradeProcess call -- "
        "the uploader is the sole PROCESSID reserver (single-writer contract)"
    )
    assert len(eq_calls) == len(EXPECTED_FULL_PIPELINE_SEQUENCE) == 7, (
        "total call count must match exactly what the sequence implies -- no extras, no gaps"
    )


# =============================================================================
# Scenario 3 — Multiple polls before EVERY polling stage succeeds.
# =============================================================================

async def test_multiple_polls_uniform_retry_behaviour_across_every_stage(cfg, session_factory, test_date):
    """
    mock_set_ready_after(3): CBOS says "not ready" twice before succeeding on
    the 3rd poll, for EVERY polling stage. Confirm the recorded sequence
    shows the SAME process_name repeated exactly 3 times in a row before
    advancing, uniformly across ALL FOUR polling stages (BeginFileUpload
    INIT check, FILEUPLOAD, BILLPOSTING, RECON, CONTRACTNOTEGENERATION) --
    not just one stage already covered by existing tests.

    Note: BeginFileUpload (INIT) is a boolean holiday gate (SKIP/TRUE/FALSE)
    that also uses _mock_ready_after's counter in the shared in-process
    mock (see CbosClient._mock_file_status) -- so it too retries exactly
    N times before returning TRUE, just like the 4 confirmation polls.
    getExistingProcessId / getNewTradeProcess(trigger) are NOT retried
    (each fires exactly once) -- only file_process_status polls repeat.
    No reserve-mode call: the uploader is the sole reserver.
    """
    POLLS_BEFORE_READY = 3
    cbos = SequenceRecordingCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    cbos.mock_set_ready_after(POLLS_BEFORE_READY)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(
        orchestrator, session_factory, test_date, max_cycles=6 * POLLS_BEFORE_READY + 10,
    )
    by_code = {r.segment_code: r for r in rows}
    assert by_code["EQ"].segment_status == SegmentStatus.COMPLETED

    eq_calls = cbos.calls_for("EQ")

    # Expected sequence: each of the 5 polling stages repeats exactly
    # POLLS_BEFORE_READY times in a row; getExistingProcessId and
    # getNewTradeProcess(trigger) fire exactly once each, uniformly.
    expected = (
        ["BeginFileUpload"] * POLLS_BEFORE_READY
        + ["getExistingProcessId"]
        + ["FILEUPLOAD"] * POLLS_BEFORE_READY
        + ["getNewTradeProcess:trigger"]
        + ["BILLPOSTING"] * POLLS_BEFORE_READY
        + ["RECON"] * POLLS_BEFORE_READY
        + ["CONTRACTNOTEGENERATION"] * POLLS_BEFORE_READY
    )
    assert eq_calls == expected, (
        f"EQ's call sequence under mock_set_ready_after({POLLS_BEFORE_READY}) does not show "
        f"uniform retry-until-ready behaviour across every polling stage.\n"
        f"Expected: {expected}\nActual:   {eq_calls}"
    )

    # Explicitly confirm each of the 5 polling stages individually shows
    # exactly POLLS_BEFORE_READY consecutive repeats (not just that the
    # overall sequence happens to match).
    for process_name in ("BeginFileUpload", "FILEUPLOAD", "BILLPOSTING", "RECON", "CONTRACTNOTEGENERATION"):
        run_length = eq_calls.count(process_name)
        assert run_length == POLLS_BEFORE_READY, (
            f"polling stage {process_name!r} expected exactly {POLLS_BEFORE_READY} calls "
            f"(retry-until-ready), got {run_length}"
        )
        # And confirm they are consecutive (no interleaving with another stage).
        first_idx = eq_calls.index(process_name)
        actual_run = eq_calls[first_idx: first_idx + POLLS_BEFORE_READY]
        assert actual_run == [process_name] * POLLS_BEFORE_READY, (
            f"{process_name!r}'s {POLLS_BEFORE_READY} calls must be consecutive before "
            f"advancing to the next stage, got interleaved sequence {eq_calls[first_idx:first_idx + POLLS_BEFORE_READY + 2]}"
        )


# =============================================================================
# Scenario 4 — Existing process_id path skips the reserve-mode call entirely.
# =============================================================================

async def test_existing_process_id_path_skips_reserve_mode_call(cfg, session_factory, test_date):
    """
    getdropdown(EXISTINGPROCESSID) reports found=True for EQ (simulating RPA
    having already reserved a PID before the agent's pipeline runs).
    Confirm the call sequence goes straight from getExistingProcessId to the
    trigger-mode getNewTradeProcess call WITHOUT an intervening reserve-mode
    (PROCESSID="0") call -- the two "get a process_id" code paths (existing
    vs. reserve-new) are mutually exclusive per segment-day, never both fire.
    """
    cbos = ExistingPidSequenceCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, existing_pid_segment="EQ", existing_pid="55555",
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert by_code["EQ"].segment_status == SegmentStatus.COMPLETED
    assert by_code["EQ"].process_id == "55555", "the pre-existing PID must be the one actually used"

    eq_calls = cbos.calls_for("EQ")

    expected = [
        "BeginFileUpload",
        "getExistingProcessId",
        # NO "getNewTradeProcess:reserve" here -- existing.found short-circuits it.
        "FILEUPLOAD",
        "getNewTradeProcess:trigger",
        "BILLPOSTING",
        "RECON",
        "CONTRACTNOTEGENERATION",
    ]
    assert eq_calls == expected, (
        f"existing-PID path must skip the reserve-mode getNewTradeProcess call entirely.\n"
        f"Expected: {expected}\nActual:   {eq_calls}"
    )
    assert "getNewTradeProcess:reserve" not in eq_calls, (
        "reserve-mode getNewTradeProcess must NEVER fire when an existing process_id was found"
    )
    # Exactly one getExistingProcessId call, exactly one trigger-mode call --
    # the two "get a process_id" paths never both fire in the same segment-day.
    assert eq_calls.count("getExistingProcessId") == 1
    assert eq_calls.count("getNewTradeProcess:trigger") == 1
    assert eq_calls.count("getNewTradeProcess:reserve") == 0


# =============================================================================
# Scenario 5 — Full day: all 9 segments + all 5 post-trade processes,
# grand-total call-count audit with no cross-segment leakage.
# =============================================================================

async def test_full_day_all_segments_and_post_trade_call_count_audit(cfg, session_factory, test_date):
    """
    A full day: some segments holiday-skipped (via a recording variant of
    SkippingCbosClient), the rest complete normally, then the full 5-process
    post-trade chain runs to completion. Produce a per-segment (and
    per-post-trade-process) call-count breakdown and confirm the GRAND TOTAL
    exactly matches the sum of each segment's own expected count -- proving
    no cross-segment call leakage in how the recording fake keys calls.
    """
    HOLIDAY_SEGMENTS = {"CUR", "MCX"}

    class RecordingMultiSkippingCbosClient(SequenceRecordingCbosClient):
        def __init__(self, status_url: str, process_url: str, *, skip_segments: set[str]):
            super().__init__(status_url, process_url)
            self._skip_segments = {s.upper() for s in skip_segments}

        async def file_process_status(self, segment: str, process_name: str, user_id: str) -> FileStatusResult:
            self.calls.append((segment.upper(), process_name))
            if segment.upper() in self._skip_segments and process_name == "BeginFileUpload":
                return FileStatusResult(response="SKIP", raw_body='{"Status":"SKIP"}', error=None, is_transient=False)
            return await CbosClient.file_process_status(self, segment, process_name, user_id)

    cbos = RecordingMultiSkippingCbosClient(
        cfg.cbos_status_url, cfg.cbos_process_url, skip_segments=HOLIDAY_SEGMENTS,
    )
    cbos.mock_set_ready_after(1)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    assert set(by_code) == set(SEGMENT_ORDER)

    await helpers.seed_post_trade_day(session_factory, test_date)
    pt_rows = await helpers.drive_post_trade_until_terminal(orchestrator, session_factory, test_date)
    pt_by_code = {r.segment_code: r for r in pt_rows}
    assert set(pt_by_code) == set(POST_TRADE_ORDER)

    # --- Build the expected per-segment call count. ---
    expected_counts: dict[str, int] = {}
    for code in SEGMENT_ORDER:
        if code in HOLIDAY_SEGMENTS:
            expected_counts[code] = 1  # exactly 1: the INIT BeginFileUpload SKIP check
        else:
            expected_counts[code] = len(EXPECTED_FULL_PIPELINE_SEQUENCE)  # 8, happy path

    # Post-trade: COLVAL/COLALLOC/MTFFT each make 1 GTG poll + 1
    # already-triggered check + 1 trigger + 1 completion poll = 4 calls on
    # the fastest happy path (mock_set_ready_after(1)). DMRPT/DMSTMT
    # (DEPENDS_ON_PREVIOUS_PROCESS=True) skip the GTG CBOS poll entirely
    # (gate is a pure DB check, no CBOS call) -- so only 3 CBOS calls each:
    # already-triggered check + trigger + completion poll.
    for code in POST_TRADE_ORDER:
        if code in ("DMRPT", "DMSTMT"):
            expected_counts[code] = 3
        else:
            expected_counts[code] = 4

    # --- Actual counts, read back from the recording fake, per segment. ---
    actual_counts = {code: len(cbos.calls_for(code)) for code in list(SEGMENT_ORDER) + list(POST_TRADE_ORDER)}

    print("\n=== CBOS call-count audit (per segment/process) ===")
    print(f"{'code':<10}{'expected':>10}{'actual':>10}")
    for code in list(SEGMENT_ORDER) + list(POST_TRADE_ORDER):
        print(f"{code:<10}{expected_counts[code]:>10}{actual_counts[code]:>10}")

    for code in list(SEGMENT_ORDER) + list(POST_TRADE_ORDER):
        assert actual_counts[code] == expected_counts[code], (
            f"{code}: expected {expected_counts[code]} CBOS calls, recorded {actual_counts[code]} -- "
            f"actual call log for {code}: {cbos.calls_for(code)}"
        )

    grand_total_expected = sum(expected_counts.values())
    grand_total_actual = len(cbos.calls)
    assert grand_total_actual == grand_total_expected, (
        f"GRAND TOTAL call count mismatch: expected sum-of-per-segment "
        f"({grand_total_expected}) != actual total calls recorded ({grand_total_actual}) -- "
        f"indicates cross-segment call leakage in how the fake keys calls, or an "
        f"unaccounted-for extra/missing call somewhere"
    )

    # No segment's calls leaked into another's: sanity-check disjointness by
    # re-deriving the total purely from per-segment breakdowns.
    assert sum(actual_counts.values()) == len(cbos.calls), (
        "sum of all per-segment/process call counts must equal the total call log length -- "
        "a mismatch would mean some call was double-counted or attributed to the wrong segment"
    )


# =============================================================================
# Scenario 6 — Post-trade "already triggered" short-circuit call count.
# =============================================================================

async def test_post_trade_already_triggered_short_circuit_zero_trigger_calls(cfg, session_factory, test_date):
    """
    COLVAL's "already triggered" check (check_collateral_valuation_triggered)
    reports already_triggered=True on its first (and only) check -- CBOS
    mock's mock_mark_already_triggered() opts a segment into this branch.
    Confirm the process goes DIRECTLY from WAITING_FOR_GTG to
    WAITING_FOR_COMPLETION WITHOUT ever calling the trigger endpoint: zero
    calls to trigger_collateral_valuation's underlying endpoint
    (GetCollateralValuation with BUTTONNAME=COLLATERAL_VALUATION_DATEWISE),
    only the GTG poll + the REFRESH-variant already-triggered check +
    the completion poll.
    """

    # SequenceRecordingCbosClient already records file_process_status,
    # getExistingProcessId, getNewTradeProcess, _post_trade_trigger, AND
    # both already-triggered-check variants (REFRESH-based and
    # file_process_status-based) -- see its definition above. Only COLVAL
    # is opted into the "already triggered" branch here; COLALLOC/MTFFT/
    # DMRPT/DMSTMT are left on the normal path (they legitimately DO fire
    # their trigger endpoints) precisely so this test can distinguish "no
    # trigger call for COLVAL specifically" from "no trigger calls fired
    # anywhere in the whole post-trade chain" (which would be the wrong,
    # over-broad claim).
    cbos = SequenceRecordingCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    cbos.mock_set_ready_after(1)
    cbos.mock_mark_already_triggered("COLVAL")
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_post_trade_day(session_factory, test_date)
    pt_rows = await helpers.drive_post_trade_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in pt_rows}

    colval = by_code["COLVAL"]
    assert colval.segment_status == SegmentStatus.COMPLETED

    # The direct-edge transition: TRIGGERED state was never entered at all,
    # so processes_json must have no TRIGGERED key (or, if AbstractStateMachine
    # always seeds top-level keys, it must show no trigger attempt recorded).
    assert SegmentState.TRIGGERED.value not in colval.processes_json, (
        "the already-triggered short-circuit must go DIRECTLY from WAITING_FOR_GTG to "
        "WAITING_FOR_COMPLETION -- TRIGGERED must never be entered, so processes_json must "
        f"have no TRIGGERED key at all, got keys={list(colval.processes_json.keys())}"
    )

    colval_calls = cbos.calls_for("COLVAL")

    # Zero calls to COLVAL's own trigger endpoint -- the whole point of this
    # test. (Other post-trade processes' trigger calls, e.g. COLALLOC's,
    # are unaffected and correctly still fire -- see the exact-sequence
    # assertion below, which proves no trigger: tag appears for COLVAL at all.)
    assert not any(tag.startswith("trigger:") for tag in colval_calls), (
        f"already_triggered=True must short-circuit straight to WAITING_FOR_COMPLETION "
        f"WITHOUT ever calling COLVAL's trigger endpoint, got calls={colval_calls}"
    )
    assert any(tag.startswith("alreadyTriggeredCheck:") for tag in colval_calls), (
        "the already-triggered REFRESH-variant check must have been called exactly once"
    )
    assert sum(1 for tag in colval_calls if tag.startswith("alreadyTriggeredCheck:")) == 1

    # Exact expected sequence: GTG poll -> already-triggered check ->
    # completion poll (same ProcessName as the GTG poll, per
    # handle_waiting_for_completion()). No reserve/trigger/getExistingProcessId
    # calls exist in the post-trade pipeline at all.
    expected = [
        "CollateralValuation",                          # WAITING_FOR_GTG poll
        "alreadyTriggeredCheck:GetCollateralValuation",  # already-triggered pre-check
        "CollateralValuation",                          # WAITING_FOR_COMPLETION poll
    ]
    assert colval_calls == expected, (
        f"COLVAL's already-triggered short-circuit call sequence mismatch.\n"
        f"Expected: {expected}\nActual:   {colval_calls}"
    )


# =============================================================================
# Scenario N — Uploader hasn't reserved yet: the agent WAITS, never mints.
# =============================================================================

async def test_agent_waits_for_uploader_reservation_and_never_mints(cfg, session_factory, test_date):
    """
    Single-writer contract (CBOS_HANDOFF_CONTRACT.md): the uploader is the
    sole PROCESSID reserver. With mock_set_uploader_reserve_delay(2) the
    first two getdropdown(EXISTINGPROCESSID) lookups miss — the agent must
    stay in WAITING_FOR_FILE_UPLOAD with no process_id, KEEP asking on later
    cycles, and resolve the PID as EXISTING once the uploader-sim provides
    it. At no point may a reserve-mode getNewTradeProcess fire.
    """
    UPLOADER_DELAY = 2
    cbos = SequenceRecordingCbosClient(cfg.cbos_status_url, cfg.cbos_process_url)
    cbos.mock_set_ready_after(1)
    cbos.mock_set_uploader_reserve_delay(UPLOADER_DELAY)
    orchestrator = EdpOrchestrator(cfg, cbos)

    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert by_code["EQ"].segment_status == SegmentStatus.COMPLETED
    eq_calls = cbos.calls_for("EQ")

    # The PID lookup repeated until the uploader-sim reserved: 2 misses + 1 hit.
    assert eq_calls.count("getExistingProcessId") == UPLOADER_DELAY + 1, (
        f"expected {UPLOADER_DELAY + 1} getExistingProcessId lookups (miss x{UPLOADER_DELAY}, "
        f"then found), got {eq_calls.count('getExistingProcessId')}:\n{eq_calls}"
    )
    # The misses must all precede FILEUPLOAD polling: no PID -> no poll yet.
    assert eq_calls.index("FILEUPLOAD") > eq_calls.index("getExistingProcessId"), eq_calls
    # And the load-bearing assertion: the agent NEVER minted.
    assert "getNewTradeProcess:reserve" not in eq_calls, (
        "agent fired a reserve-mode getNewTradeProcess while waiting for the uploader -- "
        "the single-writer contract is broken"
    )
    # The resolved PID is recorded as read-back, never as self-reserved.
    assert by_code["EQ"].processes_json["TRIGGERED"]["process_id_source"] == "EXISTING"
