"""
Unit tests for the structured log-line builders in
src/agent/edp/utils/log_fmt.py — no DB, no async, pure string formatting.

- edp_log/seg_log/stage_log/cbos_log all share the same "message then
  trailing ` | k=v k=v`" convention via the shared _kv() helper; the
  format examples below are taken verbatim from the module docstring so a
  future reformatting of any builder is caught immediately rather than
  silently drifting from the documented contract.
- _kv()'s `if v is not None` filter is the one bit of actual logic here:
  a None-valued kwarg (e.g. an optional field not yet populated) must
  disappear entirely from the log line, not render as the literal
  substring "key=None" — tested through all 4 builders, not just _kv()
  in isolation, since each builder could in principle bypass _kv().
- elapsed() is explicitly wrapped in try/except in the source specifically
  so a malformed timestamp can never raise out of a logging call site;
  both the None-argument short-circuit and the parse-failure path are
  tested to confirm that contract holds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.agent.edp.utils.log_fmt import cbos_log, edp_log, elapsed, seg_log, stage_log


def test_seg_log_matches_documented_format():
    """Docstring example: seg_log("EQ", "2026-07-01", "Segment started",
    window_start="17:00")."""
    result = seg_log("EQ", "2026-07-01", "Segment started", window_start="17:00")
    assert result == "[EDP] trade_date=2026-07-01 segment=EQ | Segment started | window_start=17:00"


def test_stage_log_matches_documented_format():
    """Docstring example: stage_log("EQ", "INIT", "poll", poll=3,
    response="FALSE")."""
    result = stage_log("EQ", "INIT", "poll", poll=3, response="FALSE")
    assert result == "[EDP] segment=EQ stage=INIT | poll | poll=3 response=FALSE"


def test_cbos_log_matches_documented_format():
    """Docstring example: cbos_log("EQ", "BeginFileUpload", "OK",
    elapsed_ms=120, response="TRUE")."""
    result = cbos_log("EQ", "BeginFileUpload", "OK", elapsed_ms=120, response="TRUE")
    assert result == "[CBOS] segment=EQ api=BeginFileUpload | OK | elapsed_ms=120 response=TRUE"


def test_edp_log_matches_generic_format_with_kwargs():
    result = edp_log("Agent starting", wake_interval=30)
    assert result == "[EDP] Agent starting | wake_interval=30"


def test_edp_log_none_kwarg_is_omitted_entirely():
    result = edp_log("Agent starting", reason=None)
    assert result == "[EDP] Agent starting"
    assert "reason" not in result
    assert "None" not in result


def test_seg_log_none_kwarg_is_omitted_entirely():
    result = seg_log("EQ", "2026-07-01", "Segment started", window_start=None)
    assert result == "[EDP] trade_date=2026-07-01 segment=EQ | Segment started"
    assert "window_start" not in result


def test_stage_log_none_kwarg_is_omitted_entirely():
    result = stage_log("EQ", "INIT", "poll", poll=3, response=None)
    assert result == "[EDP] segment=EQ stage=INIT | poll | poll=3"
    assert "response" not in result


def test_cbos_log_none_kwarg_is_omitted_entirely():
    result = cbos_log("EQ", "BeginFileUpload", "OK", elapsed_ms=None, response="TRUE")
    assert result == "[CBOS] segment=EQ api=BeginFileUpload | OK | response=TRUE"
    assert "elapsed_ms" not in result


def test_stage_log_all_kwargs_none_omits_pipe_segment_entirely():
    """When every extra kwarg is None, _kv() must produce "" (empty),
    not " | " with nothing after it — no dangling pipe."""
    result = stage_log("EQ", "INIT", "poll", poll=None, response=None)
    assert result == "[EDP] segment=EQ stage=INIT | poll"
    assert result.endswith("poll")


def test_edp_log_no_kwargs_produces_clean_message_no_trailing_pipe():
    result = edp_log("Agent starting")
    assert result == "[EDP] Agent starting"
    assert "|" not in result


def test_seg_log_no_kwargs_produces_clean_message_no_trailing_pipe():
    result = seg_log("EQ", "2026-07-01", "Segment started")
    assert result == "[EDP] trade_date=2026-07-01 segment=EQ | Segment started"
    # Only the fixed segment/date separator pipe is present — no second
    # trailing " | ..." for the (empty) kwargs.
    assert result.count("|") == 1


def test_stage_log_no_kwargs_produces_clean_message_no_trailing_pipe():
    result = stage_log("EQ", "INIT", "poll")
    assert result == "[EDP] segment=EQ stage=INIT | poll"
    assert result.count("|") == 1


def test_cbos_log_no_kwargs_produces_clean_message_no_trailing_pipe():
    result = cbos_log("EQ", "BeginFileUpload", "OK")
    assert result == "[CBOS] segment=EQ api=BeginFileUpload | OK"
    assert result.count("|") == 1


def test_elapsed_naive_timestamps_2_5_seconds_apart():
    start = "2026-06-29T10:00:00.000000"
    end = "2026-06-29T10:00:02.500000"
    assert elapsed(start, end) == "2.5s"


def test_elapsed_timezone_aware_timestamps_2_5_seconds_apart():
    """end_iso contains a "+" offset, so the source's format-detection
    branch takes the tz-aware %z path."""
    start_dt = datetime(2026, 6, 29, 10, 0, 0, tzinfo=UTC)
    end_dt = start_dt + timedelta(seconds=2.5)
    start = start_dt.isoformat()
    end = end_dt.isoformat()
    assert "+" in end
    assert elapsed(start, end) == "2.5s"


def test_elapsed_zulu_suffix_timestamps_2_5_seconds_apart():
    """end_iso ending in "Z" is the other tz-aware branch condition."""
    start = "2026-06-29T10:00:00.000000+00:00"
    end = "2026-06-29T10:00:02.500000Z"
    assert elapsed(start, end) == "2.5s"


def test_elapsed_start_none_returns_none():
    assert elapsed(None, "2026-06-29T10:00:02.500000") is None


def test_elapsed_end_none_returns_none():
    assert elapsed("2026-06-29T10:00:00.000000", None) is None


def test_elapsed_both_none_returns_none():
    assert elapsed(None, None) is None


def test_elapsed_malformed_start_returns_none_not_raises():
    assert elapsed("not-a-timestamp", "2026-06-29T10:00:02.500000") is None


def test_elapsed_malformed_end_returns_none_not_raises():
    assert elapsed("2026-06-29T10:00:00.000000", "also-not-a-timestamp") is None
