"""
Ops email alerts (src/agent/edp/alerts.py) must fire on FAILED, on a
window-deadline TIMEOUT-skip, and on any other SKIPPED outcome (CBOS_SKIP —
market holiday, or CBOS explicitly returning SKIP mid-stage) — see alerts.py's
module docstring for the reasoning. These tests spy on the three public alert
functions directly (alerts.send_failure_alert / send_timeout_alert /
send_skip_alert) rather than going through global_email_service/Graph, since
the actual send behavior (dry_run, Graph retries, etc.) is that library's own
responsibility and already covered by its own test suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.agent.edp import alerts as alerts_module
from src.agent.edp.utils.datetime_utils import now_ist
from src.agent.edp import repository
from src.agent.edp.models import SegmentPhase, SegmentStatus
from src.agent.edp.pipeline import stages
from src.agent.edp.pipeline.executor import advance_pipeline
from src.tools.cbos_client import CbosClient

from . import helpers

SEGMENT = "EQ"


class _AsyncRecorder:
    """Records every call it receives instead of doing anything real."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, row: dict) -> None:
        self.calls.append(row)


async def test_fail_helper_sends_failure_alert(monkeypatch, cfg, session_factory, test_date):
    failure_alerts = _AsyncRecorder()
    timeout_alerts = _AsyncRecorder()
    skip_alerts = _AsyncRecorder()
    monkeypatch.setattr(alerts_module, "send_failure_alert", failure_alerts)
    monkeypatch.setattr(alerts_module, "send_timeout_alert", timeout_alerts)
    monkeypatch.setattr(alerts_module, "send_skip_alert", skip_alerts)

    await helpers.seed_day(session_factory, test_date, cfg)
    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.segment_status = SegmentStatus.IN_PROGRESS
        await stages._fail(row, "CBOS_ERROR", "simulated failure for alert test", datetime.now())
        await session.commit()

    assert len(failure_alerts.calls) == 1, "_fail() must always send exactly one failure alert"
    assert timeout_alerts.calls == []
    assert skip_alerts.calls == []
    sent_row = failure_alerts.calls[0]
    assert sent_row["segment_code"] == SEGMENT
    assert sent_row["segment_status"] == "FAILED"
    assert sent_row["skip_category"] == "CBOS_ERROR"
    assert sent_row["skip_reason"] == "simulated failure for alert test"


async def test_skip_helper_sends_skip_alert(monkeypatch, cfg, session_factory, test_date):
    """CBOS_SKIP (market holiday, CBOS explicitly returning SKIP mid-stage)
    must alert too — ops wants visibility into every segment that didn't run
    to completion, not just outright FAILED/TIMEOUT ones (see alerts.py's
    module docstring)."""
    failure_alerts = _AsyncRecorder()
    timeout_alerts = _AsyncRecorder()
    skip_alerts = _AsyncRecorder()
    monkeypatch.setattr(alerts_module, "send_failure_alert", failure_alerts)
    monkeypatch.setattr(alerts_module, "send_timeout_alert", timeout_alerts)
    monkeypatch.setattr(alerts_module, "send_skip_alert", skip_alerts)

    await helpers.seed_day(session_factory, test_date, cfg)
    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.segment_status = SegmentStatus.IN_PROGRESS
        await stages._skip(row, "CBOS_SKIP", "BeginFileUpload returned SKIP — market holiday", datetime.now())
        await session.commit()

    assert failure_alerts.calls == []
    assert timeout_alerts.calls == []
    assert len(skip_alerts.calls) == 1, "_skip() must always send exactly one skip alert"
    sent_row = skip_alerts.calls[0]
    assert sent_row["segment_code"] == SEGMENT
    assert sent_row["segment_status"] == "SKIPPED"
    assert sent_row["skip_category"] == "CBOS_SKIP"


async def test_mid_run_window_deadline_sends_timeout_alert(monkeypatch, cfg, session_factory, test_date):
    """advance_pipeline()'s own window_end check (a segment stuck polling
    past its deadline) must alert — this is an operational problem, not a
    day-to-day routine outcome like a holiday.

    advance_pipeline() always compares against the real wall-clock
    now_ist() internally (refreshed every loop iteration — see its
    docstring), not the `now` argument, so the deadline here must be a
    real past timestamp rather than one anchored to test_date (which is
    thousands of days in the future — see conftest.test_date)."""
    failure_alerts = _AsyncRecorder()
    timeout_alerts = _AsyncRecorder()
    skip_alerts = _AsyncRecorder()
    monkeypatch.setattr(alerts_module, "send_failure_alert", failure_alerts)
    monkeypatch.setattr(alerts_module, "send_timeout_alert", timeout_alerts)
    monkeypatch.setattr(alerts_module, "send_skip_alert", skip_alerts)

    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    await helpers.seed_day(session_factory, test_date, cfg)
    real_past_deadline = now_ist() - timedelta(hours=1)

    async with session_factory() as session:
        row = await repository.get_one(session, test_date, SEGMENT)
        row.segment_status = SegmentStatus.IN_PROGRESS
        row.started_at = real_past_deadline
        row.current_phase = SegmentPhase.AWAIT_FILE_UPLOAD
        row.current_process = "FILEUPLOAD"
        await session.commit()

        row = await repository.get_one(session, test_date, SEGMENT)
        outcome = await advance_pipeline(
            cbos=cbos, row=row, session=session, login_id=cfg.cbos_login_id,
            now=now_ist(), window_end=real_past_deadline,
        )
        await session.commit()

    assert outcome == "skipped"
    assert len(timeout_alerts.calls) == 1
    assert failure_alerts.calls == []
    assert skip_alerts.calls == []
    sent_row = timeout_alerts.calls[0]
    assert sent_row["segment_code"] == SEGMENT
    assert sent_row["skip_category"] == "TIMEOUT"


async def test_alerts_disabled_by_default_in_tests():
    """The autouse disable_email_alerts fixture (conftest.py) must actually
    take effect, so the rest of the suite never attempts a real Graph call."""
    assert alerts_module.alerts_enabled() is False
