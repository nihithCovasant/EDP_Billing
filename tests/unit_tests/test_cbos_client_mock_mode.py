"""
Unit tests for CbosClient's use_mock=True in-process mock behavior.

These tests exercise the mock branch of every public CbosClient method —
no network calls, no database — confirming CbosClient(status_url,
process_url, use_mock=True) never makes an HTTP call and instead returns
deterministic local responses from the _mock_* implementations.

Covered:
- file_process_status() mock responses (including the BeginFileUpload/SKIP
  special case and the ready-after-N-polls TRUE transition).
- get_new_trade_process() mock PID reservation (instance-level
  itertools.count, so distinct (segment, trade_date) keys get incrementing
  PIDs, and the same key is idempotent across repeated reserve calls).
- get_existing_process_id() mock "found" semantics, gated on whether a PID
  was actually reserved first via get_new_trade_process().
- The 5 post-trade trigger methods, which all share
  _mock_post_trade_trigger() and always succeed deterministically.
- The 5 post-trade "already triggered" check methods, which default to
  already_triggered=False unless mock_mark_already_triggered() was called.
- self.use_mock is assigned exactly once, in __init__, and never
  reassigned elsewhere in the class body — regression coverage for a
  previously-audited concurrency/toggle-safety concern.
- Representative methods never reach real network code in mock mode, even
  when constructed with an obviously-unreachable status_url/process_url.
"""

from __future__ import annotations

import inspect
from datetime import date

from src.tools.cbos_client import (
    AlreadyTriggeredResult,
    CbosClient,
    ExistingProcessResult,
    FileStatusResult,
    NewTradeProcessResult,
    PostTradeTriggerResult,
)

UNREACHABLE_STATUS_URL = "http://this-host-does-not-exist-12345.invalid"
UNREACHABLE_PROCESS_URL = "http://this-host-does-not-exist-12345.invalid"


# =============================================================================
# 1. file_process_status
# =============================================================================


async def test_file_process_status_begin_file_upload_returns_expected_mock_value():
    """
    _mock_file_status() returns SKIP for BeginFileUpload only when the
    segment code contains "SKIP"; otherwise it follows the normal
    poll-counter FALSE-until-ready-after-N-calls TRUE path. A single call
    with a plain segment ("EQ") must complete instantly (no exception) and
    report FALSE, since _mock_ready_after defaults to 2 and this is call #1.
    """
    cbos = CbosClient("http://status", "http://process", use_mock=True)

    result = await cbos.file_process_status(segment="EQ", process_name="BeginFileUpload", user_id="CV0001")

    assert isinstance(result, FileStatusResult)
    assert result.error is None
    assert result.response in ("TRUE", "FALSE", "SKIP")
    assert result.response == "FALSE"


async def test_file_process_status_skip_segment_returns_skip():
    """A segment code containing "SKIP" short-circuits BeginFileUpload to SKIP,
    regardless of poll count — simulates the holiday gate."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)

    result = await cbos.file_process_status(segment="EQ_SKIP", process_name="BeginFileUpload", user_id="CV0001")

    assert result.response == "SKIP"
    assert result.is_skip is True


async def test_file_process_status_becomes_ready_after_configured_poll_count():
    """
    _mock_ready_after defaults to 2: the 1st call for a given
    (segment, process_name) key returns FALSE, and the 2nd+ call returns
    TRUE. This is the poll-until-ready pattern used by FILEUPLOAD/
    BILLPOSTING/RECON/CONTRACTNOTEGENERATION.
    """
    cbos = CbosClient("http://status", "http://process", use_mock=True)

    first = await cbos.file_process_status(segment="EQ", process_name="FILEUPLOAD", user_id="CV0001")
    second = await cbos.file_process_status(segment="EQ", process_name="FILEUPLOAD", user_id="CV0001")

    assert first.response == "FALSE"
    assert first.is_pending is True
    assert second.response == "TRUE"
    assert second.is_ready is True


# =============================================================================
# 2. get_new_trade_process
# =============================================================================


async def test_get_new_trade_process_reserve_returns_success_with_nonempty_pid():
    """process_id="0" (reserve mode) must return success=True with a
    non-empty process_id/PID value."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)

    result = await cbos.get_new_trade_process("EQ", "CV0001", date(2026, 6, 29), process_id="0")

    assert isinstance(result, NewTradeProcessResult)
    assert result.success is True
    assert result.process_id
    assert result.process_id != ""


async def test_get_new_trade_process_reserve_is_idempotent_for_same_key():
    """
    _mock_reserved_pids is an instance-level dict keyed by
    (segment.upper(), trade_date.isoformat()) — a second reserve call
    (process_id="0") for the SAME key on the SAME instance must return the
    identical PID (mirrors real CBOS: once reserved, a PROCESSID persists
    for that segment+date), not a freshly incremented one.
    """
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    trade_date = date(2026, 6, 29)

    first = await cbos.get_new_trade_process("EQ", "CV0001", trade_date, process_id="0")
    second = await cbos.get_new_trade_process("EQ", "CV0001", trade_date, process_id="0")

    assert first.process_id == second.process_id


async def test_get_new_trade_process_reserve_increments_pid_counter_across_distinct_keys():
    """
    _mock_pid_counter is an itertools.count(17001) created once in
    __init__ (instance-level, not module-level) and shared across all
    (segment, trade_date) keys on that instance. Two DIFFERENT keys on the
    SAME instance must therefore get distinct, incrementing PIDs.
    """
    cbos = CbosClient("http://status", "http://process", use_mock=True)

    result_eq = await cbos.get_new_trade_process("EQ", "CV0001", date(2026, 6, 29), process_id="0")
    result_fo = await cbos.get_new_trade_process("FO", "CV0001", date(2026, 6, 29), process_id="0")

    assert result_eq.process_id != result_fo.process_id
    assert int(result_fo.process_id) > int(result_eq.process_id)


async def test_get_new_trade_process_fresh_instances_start_pid_counter_at_same_seed():
    """
    Confirms _mock_pid_counter is instance-level, not a shared module-level
    itertools.count: two FRESH CbosClient instances each reserving a PID
    for the same key independently must produce the SAME deterministic
    first value (17001), because each instance seeds its own counter from
    scratch rather than sharing state with any other instance.
    """
    cbos_a = CbosClient("http://status", "http://process", use_mock=True)
    cbos_b = CbosClient("http://status", "http://process", use_mock=True)
    trade_date = date(2026, 6, 29)

    result_a = await cbos_a.get_new_trade_process("EQ", "CV0001", trade_date, process_id="0")
    result_b = await cbos_b.get_new_trade_process("EQ", "CV0001", trade_date, process_id="0")

    assert result_a.process_id == result_b.process_id == "17001"


async def test_get_new_trade_process_trigger_mode_first_call_has_empty_steps():
    """process_id=<actual PID> (trigger mode): the 1st call for a given
    (segment, trade_date) key returns an empty Table2 -> steps == []."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)

    result = await cbos.get_new_trade_process("EQ", "CV0001", date(2026, 6, 29), process_id="17001")

    assert result.success is True
    assert result.process_id == "17001"
    assert result.steps == []


async def test_get_new_trade_process_trigger_mode_second_call_reports_in_progress_step():
    """The 2nd+ trigger-mode call for the same key returns Table2 with one
    step whose status is IN_PROGRESS — used to exercise the TRIGGERING
    recovery decision tree."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    trade_date = date(2026, 6, 29)

    await cbos.get_new_trade_process("EQ", "CV0001", trade_date, process_id="17001")
    second = await cbos.get_new_trade_process("EQ", "CV0001", trade_date, process_id="17001")

    assert len(second.steps) == 1
    assert second.steps[0].status == "IN_PROGRESS"
    assert second.steps[0].name == "TRADE_MERGER"


# =============================================================================
# 3. get_existing_process_id
# =============================================================================


async def test_get_existing_process_id_uploader_sim_miss_then_provision():
    """The mock plays the UPLOADER (the sole PID reserver): with
    mock_set_uploader_reserve_delay(1) the first lookup for a
    (segment, trade_date) must correctly miss (found=False, no
    process_id), and the next lookup must return a provisioned PID —
    as if the uploader reserved it between the agent's cycles. The same
    PID is then stable across further lookups."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    cbos.mock_set_uploader_reserve_delay(1)
    trade_date = date(2026, 6, 29)

    first = await cbos.get_existing_process_id("EQ", "CV0001", trade_date)
    assert isinstance(first, ExistingProcessResult)
    assert first.found is False
    assert first.process_id is None

    second = await cbos.get_existing_process_id("EQ", "CV0001", trade_date)
    assert second.found is True
    assert second.process_id

    third = await cbos.get_existing_process_id("EQ", "CV0001", trade_date)
    assert third.process_id == second.process_id


async def test_get_existing_process_id_found_after_reservation():
    """After get_new_trade_process(process_id="0") reserves a PID for a
    given (segment, trade_date), get_existing_process_id() for the same
    key must report found=True with that PID and a non-empty description."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    trade_date = date(2026, 6, 29)

    reserved = await cbos.get_new_trade_process("EQ", "CV0001", trade_date, process_id="0")
    result = await cbos.get_existing_process_id("EQ", "CV0001", trade_date)

    assert result.found is True
    assert result.process_id == reserved.process_id
    assert result.description
    assert reserved.process_id in result.description


# =============================================================================
# 4. Post-trade trigger methods (5) — all mock-succeed via
#    _mock_post_trade_trigger()
# =============================================================================


async def test_trigger_collateral_valuation_mock_succeeds():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.trigger_collateral_valuation("CV0001", date(2026, 6, 29))
    assert isinstance(result, PostTradeTriggerResult)
    assert result.success is True
    assert result.message == "Process started successfully"


async def test_trigger_collateral_allocation_mock_succeeds():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.trigger_collateral_allocation("CV0001", date(2026, 6, 29))
    assert isinstance(result, PostTradeTriggerResult)
    assert result.success is True
    assert result.message == "Process started successfully"


async def test_trigger_mtf_fund_transfer_mock_succeeds():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.trigger_mtf_fund_transfer("CV0001", date(2026, 6, 29))
    assert isinstance(result, PostTradeTriggerResult)
    assert result.success is True
    assert result.message == "Process started successfully"


async def test_trigger_daily_margin_reporting_mock_succeeds():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.trigger_daily_margin_reporting("CV0001", date(2026, 6, 29))
    assert isinstance(result, PostTradeTriggerResult)
    assert result.success is True
    assert result.message == "Process started successfully"


async def test_trigger_daily_margin_statements_mock_succeeds():
    """
    Unlike the other 4 post-trade triggers (which go through
    _trigger_post_trade_job()'s mock, always success=True on the first
    call), DMSTMT's trigger reuses file_process_status() — see
    trigger_daily_margin_statements()'s docstring for why — whose mock
    simulates "not ready yet" (MSG "FALSE") for the first
    (mock_ready_after - 1) calls before returning "TRUE". mock_set_ready_after(1)
    here makes it succeed on the first call, matching the other 4 triggers'
    test shape; test_trigger_daily_margin_statements_mock_not_yet_ready
    below covers the FALSE-on-first-call case explicitly.
    """
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    cbos.mock_set_ready_after(1)
    result = await cbos.trigger_daily_margin_statements("CV0001", date(2026, 6, 29))
    assert isinstance(result, PostTradeTriggerResult)
    assert result.success is True
    assert result.message == "TRUE"


async def test_trigger_daily_margin_statements_mock_not_yet_ready():
    """With the mock's default mock_ready_after=2, the first call to the
    reused file_process_status() mock returns MSG "FALSE" — trigger_daily_margin_statements()
    must surface that as success=False (retryable next cycle), not raise or
    misreport it as an error."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.trigger_daily_margin_statements("CV0001", date(2026, 6, 29))
    assert isinstance(result, PostTradeTriggerResult)
    assert result.success is False
    assert result.message == "FALSE"
    assert result.error is None


# =============================================================================
# 5. Post-trade "already triggered" check methods (5) — all default to
#    already_triggered=False via _mock_already_triggered() unless
#    mock_mark_already_triggered() was called first.
# =============================================================================


async def test_check_collateral_valuation_triggered_defaults_to_not_triggered():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.check_collateral_valuation_triggered("CV0001", date(2026, 6, 29))
    assert isinstance(result, AlreadyTriggeredResult)
    assert result.already_triggered is False


async def test_check_collateral_allocation_triggered_defaults_to_not_triggered():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.check_collateral_allocation_triggered("CV0001", date(2026, 6, 29))
    assert isinstance(result, AlreadyTriggeredResult)
    assert result.already_triggered is False


async def test_check_mtf_fund_transfer_triggered_defaults_to_not_triggered():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.check_mtf_fund_transfer_triggered("CV0001", date(2026, 6, 29))
    assert isinstance(result, AlreadyTriggeredResult)
    assert result.already_triggered is False


async def test_check_daily_margin_reporting_triggered_defaults_to_not_triggered():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.check_daily_margin_reporting_triggered("CV0001", date(2026, 6, 29))
    assert isinstance(result, AlreadyTriggeredResult)
    assert result.already_triggered is False


async def test_check_daily_margin_statements_triggered_defaults_to_not_triggered():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    result = await cbos.check_daily_margin_statements_triggered("CV0001", date(2026, 6, 29))
    assert isinstance(result, AlreadyTriggeredResult)
    assert result.already_triggered is False


async def test_check_collateral_valuation_triggered_true_after_mock_mark():
    """mock_mark_already_triggered("COLVAL") opts the segment into the
    direct-edge branch — the next check must report already_triggered=True."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    cbos.mock_mark_already_triggered("COLVAL")

    result = await cbos.check_collateral_valuation_triggered("CV0001", date(2026, 6, 29))

    assert result.already_triggered is True


async def test_check_daily_margin_statements_triggered_true_after_mock_mark():
    """Same opt-in behavior for one of the file_process_status-backed checks
    (DMSTMT), confirming _mock_already_triggered() -- not the FILEUPLOAD-style
    poll counter -- governs these 3 checks."""
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    cbos.mock_mark_already_triggered("DMSTMT")

    result = await cbos.check_daily_margin_statements_triggered("CV0001", date(2026, 6, 29))

    assert result.already_triggered is True


# =============================================================================
# 6. self.use_mock is a plain instance attribute set exactly once, in
#    __init__, and never reassigned anywhere else in the class body.
# =============================================================================


def test_use_mock_assigned_exactly_once_in_init():
    """
    Regression coverage for a previously-audited concurrency/toggle-safety
    concern: the mock/real decision must be made once at construction and
    never re-evaluated mid-run. Source-inspects CbosClient's class body for
    every "self.use_mock =" assignment; exactly one must exist, and it must
    be inside __init__.
    """
    source = inspect.getsource(CbosClient)
    assignment_lines = [
        line.strip() for line in source.splitlines() if "self.use_mock" in line and "=" in line and "==" not in line
    ]

    assert len(assignment_lines) == 1, f"expected exactly one self.use_mock assignment, found: {assignment_lines}"
    assert assignment_lines[0] == "self.use_mock = use_mock"

    init_source = inspect.getsource(CbosClient.__init__)
    assert "self.use_mock = use_mock" in init_source


def test_use_mock_reads_back_as_true_when_constructed_with_use_mock_true():
    cbos = CbosClient("http://status", "http://process", use_mock=True)
    assert cbos.use_mock is True


# =============================================================================
# 7. Mock branch never reaches real network code, even with an obviously
#    unreachable status_url/process_url -- proven by completing without
#    raising any httpx/network exception.
# =============================================================================


async def test_file_process_status_never_hits_network_with_unreachable_url():
    cbos = CbosClient(UNREACHABLE_STATUS_URL, UNREACHABLE_PROCESS_URL, use_mock=True)

    result = await cbos.file_process_status(segment="EQ", process_name="BeginFileUpload", user_id="CV0001")

    assert isinstance(result, FileStatusResult)
    assert result.error is None


async def test_get_new_trade_process_never_hits_network_with_unreachable_url():
    cbos = CbosClient(UNREACHABLE_STATUS_URL, UNREACHABLE_PROCESS_URL, use_mock=True)

    result = await cbos.get_new_trade_process("EQ", "CV0001", date(2026, 6, 29), process_id="0")

    assert isinstance(result, NewTradeProcessResult)
    assert result.success is True
    assert result.error is None


async def test_trigger_collateral_valuation_never_hits_network_with_unreachable_url():
    cbos = CbosClient(UNREACHABLE_STATUS_URL, UNREACHABLE_PROCESS_URL, use_mock=True)

    result = await cbos.trigger_collateral_valuation("CV0001", date(2026, 6, 29))

    assert isinstance(result, PostTradeTriggerResult)
    assert result.success is True
    assert result.error is None
