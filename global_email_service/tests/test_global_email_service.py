"""
Tests for the global_email_service package — fully standalone, no DB and no
real network calls. Microsoft Graph sending itself is exercised by
monkeypatching httpx.post so we can assert retry/non-retry behaviour without
a real tenant.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from global_email_service import (
    EmailSendError,
    EmailServiceConfig,
    InvalidPayloadError,
    graph_client,
    send_alert_email,
    send_segment_alert,
)
from global_email_service.colors import resolve_row_style
from global_email_service.service import parse_payload
from global_email_service.table_renderer import (
    DEFAULT_SEGMENT_COLUMNS,
    _now_str,
    derive_columns,
    render_email_body,
    resolve_severity,
)

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

MCX_RECON_ROW = {
    "trade_date": "2026-07-04",
    "segment_code": "MCX",
    "segment_name": "MCX Commodity",
    "sequence_order": 6,
    "segment_status": "FAILED",
    "current_process": "RECON",
    "current_state": "WAITING_FOR_RECON",
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


def graph_config(**overrides) -> EmailServiceConfig:
    """A non-dry-run config with fake Graph credentials, for send-path tests."""
    cfg = EmailServiceConfig(
        dry_run=False,
        default_to=["mofsl-ops@example.com"],
        graph_tenant_id="fake-tenant",
        graph_client_id="fake-client",
        graph_client_secret="fake-secret",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _fake_response(status_code: int, json_body: dict | None = None, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(status_code=status_code, json=lambda: json_body or {}, text=text)


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
    assert "MCX" in html_body and "Reconciliation" in html_body
    assert "#f8d7da" in html_body
    assert "Failed" in html_body
    assert "Completion" in html_body
    assert "Remarks" in html_body
    assert "RECON check error" in text_body


def test_render_email_body_multi_row_day_summary_flattens_nested_values():
    rows = [
        {"segment_code": "EQ", "segment_status": "COMPLETED"},
        MCX_RECON_ROW,
        {
            "segment_code": "MF",
            "segment_status": "PENDING",
            "processes_json": {"holiday_check": "done", "trigger": "pending"},
        },
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


def test_derive_columns_never_shows_sequence_order_or_skip_category():
    assert "sequence_order" not in DEFAULT_SEGMENT_COLUMNS
    assert "skip_category" not in DEFAULT_SEGMENT_COLUMNS
    assert "sequence_order" not in derive_columns([MCX_RECON_ROW])
    assert "skip_category" not in derive_columns([MCX_RECON_ROW])
    html_body, text_body = render_email_body([MCX_RECON_ROW])
    assert "Sequence Order" not in html_body
    assert "Sequence Order" not in text_body
    assert "Outcome" not in html_body
    assert "Skip Category" not in html_body


def test_explicit_columns_still_drop_sequence_order_and_skip_category():
    html_body, _text_body = render_email_body(
        [MCX_RECON_ROW],
        columns=["segment_code", "sequence_order", "skip_category", "segment_status", "current_process"],
    )
    assert "Sequence Order" not in html_body
    assert "Outcome" not in html_body
    assert "Skip Category" not in html_body
    assert "Process" in html_body


def test_minimal_payload_email_body_shows_placeholder_for_missing_timing():
    minimal_row = {"segment_code": "MCX", "segment_status": "FAILED", "skip_reason": "x"}
    html_body, text_body = render_email_body([minimal_row])
    assert "Started At" in html_body and "Completed At" in html_body
    assert "Started At" in text_body and "Completed At" in text_body


def test_customer_facing_status_labels():
    html_body, _ = render_email_body(
        [
            {"segment_code": "EQ", "segment_status": "COMPLETED"},
            {"segment_code": "MCX", "segment_status": "FAILED"},
        ]
    )
    assert "Succeeded" in html_body
    assert "Failed" in html_body
    assert "COMPLETED" not in html_body
    assert ">FAILED<" not in html_body


def test_customer_facing_column_headers_and_state_labels():
    html_body, _ = render_email_body([MCX_RECON_ROW])
    assert "Remarks" in html_body
    assert "Stage" in html_body
    assert "Outcome" not in html_body
    assert "Skip Category" not in html_body
    assert "Skip Reason" not in html_body
    assert "Current State" not in html_body
    assert "Completion" in html_body
    assert "Timed Out" not in html_body
    assert "WAITING_FOR_RECON" not in html_body


def test_stage_good_to_go_for_file_upload_failure():
    row = {
        "segment_code": "SL",
        "segment_status": "FAILED",
        "current_process": "FILEUPLOAD",
        "current_state": "WAITING_FOR_FILE_UPLOAD",
    }
    html_body, _ = render_email_body([row])
    assert "Good to Go" in html_body
    assert "Timed Out" not in html_body


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


def test_now_str_is_short_ist_form_not_raw_isoformat():
    """Footer 'Generated ...' timestamp must match the row-level style
    ('YYYY-MM-DD HH:MM:SS IST') — not the previous raw datetime.isoformat()
    with microseconds + numeric UTC offset (e.g. '...112136+05:30')."""
    import re

    result = _now_str()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} IST", result), result
    assert "+" not in result
    assert "." not in result


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
# Microsoft Graph send path — token acquisition + sendMail, with retry/
# non-retry classification (401/403/400/404/422 fail fast; everything else
# transient is retried up to max_retries).
# ---------------------------------------------------------------------------


def test_non_transient_graph_auth_failure_is_not_retried(monkeypatch):
    graph_client._token_cache.clear()
    call_count = {"n": 0}

    def fake_post(url, **kwargs):
        if "oauth2" in url:
            return _fake_response(200, {"access_token": "fake-token", "expires_in": 3600})
        call_count["n"] += 1
        return _fake_response(401, text="Unauthorized")

    monkeypatch.setattr(graph_client.httpx, "post", fake_post)

    config = graph_config(max_retries=3)
    with pytest.raises(EmailSendError):
        send_segment_alert(MCX_RECON_ROW, config=config)

    assert call_count["n"] == 1, "non-transient Graph error (401) must NOT be retried"


def test_transient_graph_error_is_retried_then_succeeds(monkeypatch):
    graph_client._token_cache.clear()
    call_count = {"n": 0}

    def flaky_post(url, **kwargs):
        if "oauth2" in url:
            return _fake_response(200, {"access_token": "fake-token", "expires_in": 3600})
        call_count["n"] += 1
        if call_count["n"] < 3:
            return _fake_response(503, text="Service Unavailable")
        return _fake_response(202)

    monkeypatch.setattr(graph_client.httpx, "post", flaky_post)
    monkeypatch.setattr(graph_client.time, "sleep", lambda _seconds: None)

    config = graph_config(max_retries=3)
    result = send_segment_alert(MCX_RECON_ROW, config=config)

    assert result.success is True
    assert call_count["n"] == 3


def test_transient_graph_error_exhausting_retries_raises_email_send_error(monkeypatch):
    graph_client._token_cache.clear()

    def always_fails(url, **kwargs):
        if "oauth2" in url:
            return _fake_response(200, {"access_token": "fake-token", "expires_in": 3600})
        return _fake_response(503, text="Service Unavailable")

    monkeypatch.setattr(graph_client.httpx, "post", always_fails)
    monkeypatch.setattr(graph_client.time, "sleep", lambda _seconds: None)

    config = graph_config(max_retries=2)
    with pytest.raises(EmailSendError):
        send_segment_alert(MCX_RECON_ROW, config=config)


def test_send_alert_email_raises_when_graph_not_configured():
    config = dry_run_config(dry_run=False)  # no graph_tenant_id/client_id/client_secret
    with pytest.raises(EmailSendError):
        send_segment_alert(MCX_RECON_ROW, config=config)
