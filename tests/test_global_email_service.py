"""
Tests for src/global_email_service — fully standalone, no DB and no real
network calls. SMTP sending itself is exercised by monkeypatching
smtp_client._send_once so we can assert retry/non-retry behaviour without
a real mail server.
"""

from __future__ import annotations

import smtplib
from pathlib import Path

import pytest

from src.global_email_service import (
    EmailSendError,
    EmailServiceConfig,
    InvalidPayloadError,
    send_alert_email,
    send_segment_alert,
)
from src.global_email_service import smtp_client
from src.global_email_service.colors import resolve_row_style
from src.global_email_service.service import parse_payload
from src.global_email_service.table_renderer import (
    DEFAULT_SEGMENT_COLUMNS,
    derive_columns,
    render_email_body,
    resolve_severity,
)

EXAMPLES_DIR = Path(__file__).parent.parent / "src" / "global_email_service" / "examples"

MCX_RECON_ROW = {
    "trade_date": "2026-07-04",
    "segment_code": "MCX",
    "segment_name": "MCX Commodity",
    "sequence_order": 6,
    "segment_status": "FAILED",
    "current_process": "RECON",
    "current_phase": "AWAIT_RECON",
    "process_id": "17006",
    "skip_category": "CBOS_ERROR",
    "skip_reason": "RECON check error: CBOS Status=RECON_MISMATCH — reconciliation could not be completed",
    "started_at": "2026-07-04T17:18:03+05:30",
    "completed_at": "2026-07-04T17:26:40+05:30",
}


def dry_run_config(**overrides) -> EmailServiceConfig:
    cfg = EmailServiceConfig(dry_run=True, default_to=["mofsl-ops@example.com"])
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def test_parse_payload_accepts_row_rows_and_flat_shapes():
    assert parse_payload({"row": MCX_RECON_ROW}).rows == [MCX_RECON_ROW]
    assert parse_payload({"rows": [MCX_RECON_ROW]}).rows == [MCX_RECON_ROW]
    flat = {**MCX_RECON_ROW, "to": ["x@example.com"], "subject": "s"}
    parsed = parse_payload(flat)
    assert parsed.rows == [MCX_RECON_ROW]
    assert parsed.to == ["x@example.com"]
    assert parsed.subject == "s"


def test_parse_payload_rejects_empty_payload():
    with pytest.raises(InvalidPayloadError):
        parse_payload({"to": ["x@example.com"]})  # no row data at all


def test_parse_payload_rejects_non_dict_rows_entries():
    with pytest.raises(InvalidPayloadError):
        parse_payload({"rows": [MCX_RECON_ROW, "not-a-dict"]})


# ---------------------------------------------------------------------------
# Color resolution — the "kind of alert" row coloring requirement
# ---------------------------------------------------------------------------

def test_failed_row_resolves_to_red():
    style = resolve_row_style(MCX_RECON_ROW)
    assert style.label == "FAILED"
    assert style.background == "#f8d7da"  # red


def test_completed_row_resolves_to_green():
    style = resolve_row_style({"segment_status": "COMPLETED"})
    assert style.label == "COMPLETED"
    assert style.background == "#d1e7dd"  # green


def test_pending_row_resolves_to_blue():
    style = resolve_row_style({"segment_status": "PENDING"})
    assert style.background == "#cfe2ff"  # blue


def test_explicit_color_override_wins():
    style = resolve_row_style({"segment_status": "COMPLETED", "color": "#123456"})
    assert style.background == "#123456"


# ---------------------------------------------------------------------------
# Rendering — MCX RECON failure scenario from the example payloads
# ---------------------------------------------------------------------------

def test_render_email_body_single_row_mcx_recon_failure():
    html_body, text_body = render_email_body([MCX_RECON_ROW], title="EDP Alert")
    assert "MCX" in html_body and "RECON" in html_body
    assert "#f8d7da" in html_body  # the FAILED row must actually be colored red
    assert "FAILED" in text_body
    assert "RECON check error" in text_body


def test_render_email_body_multi_row_day_summary_flattens_nested_values():
    rows = [
        {"segment_code": "EQ", "segment_status": "COMPLETED"},
        MCX_RECON_ROW,
        {"segment_code": "MF", "segment_status": "PENDING",
         "processes_json": {"holiday_check": "done", "trigger": "pending"}},
    ]
    html_body, text_body = render_email_body(rows)
    assert html_body.count("<tr") == 4  # header row (thead) + 3 body rows
    assert "holiday_check: done" in text_body  # nested dict flattened, not raw JSON


# ---------------------------------------------------------------------------
# Default columns — timing/process context must show up even for a minimal
# payload, since this is a customer-facing email (regression test for the
# "I don't see timings" bug report).
# ---------------------------------------------------------------------------

def test_derive_columns_uses_default_segment_columns_for_minimal_payload():
    minimal_row = {"segment_code": "MCX", "segment_status": "FAILED", "skip_reason": "x"}
    cols = derive_columns([minimal_row])
    assert cols == DEFAULT_SEGMENT_COLUMNS
    assert "started_at" in cols and "completed_at" in cols and "current_process" in cols


def test_derive_columns_appends_extra_keys_after_defaults():
    row = {"segment_code": "MCX", "segment_status": "FAILED", "custom_field": "abc"}
    cols = derive_columns([row])
    assert cols[: len(DEFAULT_SEGMENT_COLUMNS)] == DEFAULT_SEGMENT_COLUMNS
    assert cols[-1] == "custom_field"


def test_derive_columns_falls_back_to_discovery_for_non_segment_rows():
    rows = [{"name": "job-1", "status": "OK"}, {"name": "job-2", "status": "FAILED", "extra": 1}]
    assert derive_columns(rows) == ["name", "status", "extra"]


def test_derive_columns_never_shows_sequence_order():
    assert "sequence_order" not in DEFAULT_SEGMENT_COLUMNS
    assert "sequence_order" not in derive_columns([MCX_RECON_ROW])
    html_body, text_body = render_email_body([MCX_RECON_ROW])
    assert "Sequence Order" not in html_body
    assert "Sequence Order" not in text_body


def test_explicit_columns_still_drop_sequence_order():
    html_body, text_body = render_email_body(
        [MCX_RECON_ROW],
        columns=["segment_code", "sequence_order", "segment_status", "current_process"],
    )
    assert "Sequence Order" not in html_body
    assert "Sequence Order" not in text_body
    assert "Current Process" in html_body


def test_minimal_payload_email_body_shows_placeholder_for_missing_timing():
    minimal_row = {"segment_code": "MCX", "segment_status": "FAILED", "skip_reason": "x"}
    html_body, text_body = render_email_body([minimal_row])
    assert "Started At" in html_body and "Completed At" in html_body
    assert "Started At" in text_body and "Completed At" in text_body


# ---------------------------------------------------------------------------
# Severity banner — worst status across all rows drives the top-of-email
# banner shown to the reader.
# ---------------------------------------------------------------------------

def test_resolve_severity_failed_row_gives_action_required_red():
    severity = resolve_severity([MCX_RECON_ROW, {"segment_status": "COMPLETED"}])
    assert "ACTION REQUIRED" in severity.label
    assert severity.background == "#f8d7da"  # red


def test_resolve_severity_all_completed_gives_all_clear_green():
    severity = resolve_severity([{"segment_status": "COMPLETED"}, {"segment_status": "COMPLETED"}])
    assert "ALL CLEAR" in severity.label
    assert severity.background == "#d1e7dd"  # green


def test_render_email_body_includes_severity_banner():
    html_body, text_body = render_email_body([MCX_RECON_ROW])
    assert "ACTION REQUIRED" in html_body
    assert "ACTION REQUIRED" in text_body


def test_example_payload_files_parse_and_render():
    import json
    for name in (
        "sample_cash_all_passed.json",
        "sample_slbm_gtg_failed.json",
        "sample_mcx_recon_failure.json",
    ):
        payload = json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))
        request = parse_payload(payload)
        html_body, text_body = render_email_body(
            request.rows,
            title=request.title,
            summary=request.summary,
            columns=request.columns,
        )
        assert html_body and text_body
        assert "Sequence Order" not in html_body


# ---------------------------------------------------------------------------
# send_alert_email / send_segment_alert — dry-run path (no network)
# ---------------------------------------------------------------------------

def test_send_segment_alert_mcx_recon_failure_dry_run():
    result = send_segment_alert(MCX_RECON_ROW, config=dry_run_config())
    assert result.success is True
    assert result.dry_run is True
    assert "MCX" in result.subject
    assert "FAILED" in result.subject


def test_send_alert_email_multi_row_dry_run_default_subject_breakdown():
    rows = [
        {"segment_code": "EQ", "segment_status": "COMPLETED"},
        MCX_RECON_ROW,
        {"segment_code": "NSECOM", "segment_status": "PENDING"},
    ]
    result = send_alert_email({"rows": rows}, config=dry_run_config())
    assert result.success is True
    assert "3 record(s)" in result.subject
    assert "1 FAILED" in result.subject


def test_send_alert_email_requires_recipients_when_none_configured():
    with pytest.raises(InvalidPayloadError):
        send_alert_email({"row": MCX_RECON_ROW}, config=dry_run_config(default_to=[]))


# ---------------------------------------------------------------------------
# SMTP retry/non-retry classification (regression test for the
# SMTPException/OSError ordering bug — SMTPException subclasses OSError,
# so a naive `except OSError` before `except smtplib.SMTPException` would
# incorrectly treat auth failures as retryable transient errors)
# ---------------------------------------------------------------------------

def test_non_transient_smtp_auth_failure_is_not_retried(monkeypatch):
    call_count = {"n": 0}

    def fake_send_once(config, msg, all_recipients):
        call_count["n"] += 1
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtp_client, "_send_once", fake_send_once)

    config = dry_run_config(dry_run=False, max_retries=3)
    with pytest.raises(EmailSendError):
        send_segment_alert(MCX_RECON_ROW, config=config)

    assert call_count["n"] == 1, "non-transient SMTPException must NOT be retried"


def test_transient_connection_error_is_retried_then_succeeds(monkeypatch):
    call_count = {"n": 0}

    def flaky_send_once(config, msg, all_recipients):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ConnectionError("connection refused")
        return None  # third attempt succeeds

    monkeypatch.setattr(smtp_client, "_send_once", flaky_send_once)
    monkeypatch.setattr(smtp_client.time, "sleep", lambda _seconds: None)

    config = dry_run_config(dry_run=False, max_retries=3)
    result = send_segment_alert(MCX_RECON_ROW, config=config)

    assert result.success is True
    assert call_count["n"] == 3


def test_transient_error_exhausting_retries_raises_email_send_error(monkeypatch):
    def always_fails(config, msg, all_recipients):
        raise TimeoutError("timed out")

    monkeypatch.setattr(smtp_client, "_send_once", always_fails)
    monkeypatch.setattr(smtp_client.time, "sleep", lambda _seconds: None)

    config = dry_run_config(dry_run=False, max_retries=2)
    with pytest.raises(EmailSendError):
        send_segment_alert(MCX_RECON_ROW, config=config)
