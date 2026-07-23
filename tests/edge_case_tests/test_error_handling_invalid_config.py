"""
Error-handling / invalid-input tests for src/agent/edp/config.py.

Feeds nonsensical/malformed values into load_edp_config() /
build_default_workflow_json() to check each ends in one of three outcomes:
(1) raises cleanly, (2) raises cryptically, or (3) is silently accepted.
Several "silently accepted" cases were subsequently fixed (see config.py's
_validate_database_url / _validate_process_entries / _env_nonempty and the
wake-interval / cutoff-hour range checks) and those tests now assert the
fixed behavior. The remaining "raises cryptically" cases are left as-is —
they already fail loudly at/near config-load time.

Complements tests/test_config_loading.py (WARNING/EDP_STRICT_CONFIG paths)
and tests/unit_tests/test_config_env_precedence.py (resolution order); this
file only covers invalid/nonsensical input.

load_agent_config() is monkeypatched per test; env vars are cleared before
each test for isolation.
"""

from __future__ import annotations

import pytest

import src.agent.edp.config as edp_config
from src.agent.edp.config import build_default_workflow_json

_ENV_KEYS = (
    "DATABASE_URL",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USERNAME",
    "DB_PASSWORD",
    "CBOS_STATUS_URL",
    "CBOS_PROCESS_URL",
    "CBOS_USE_MOCK",
    "EDP_STRICT_CONFIG",
    "CBOS_LOGIN_ID",
    "POST_TRADE_LOGIN_ID",
    "EDP_WAKE_INTERVAL_SECONDS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def _stub_config(monkeypatch, edp_raw=None):
    """Wire up a minimal valid agent_config.json (edp section as given) plus
    a valid DATABASE_URL, so only the field under test is nonsensical."""
    monkeypatch.setattr(
        edp_config,
        "load_agent_config",
        lambda: {"default": {"edp": edp_raw or {}}},
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")


# ---------------------------------------------------------------------------
# (a) EDP_WAKE_INTERVAL_SECONDS invalid values.
# ---------------------------------------------------------------------------


def test_wake_interval_non_numeric_raises_uncaught_value_error(clean_env):
    """A non-numeric EDP_WAKE_INTERVAL_SECONDS raises ValueError uncaught
    (no try/except wraps the int() call). CPython's message names the bad
    value but not which env var produced it — cryptic but fails loudly."""
    _stub_config(clean_env)
    clean_env.setenv("EDP_WAKE_INTERVAL_SECONDS", "not_a_number")

    with pytest.raises(ValueError, match="invalid literal for int"):
        edp_config.load_edp_config()


def test_wake_interval_zero_now_raises(clean_env):
    """load_edp_config() rejects wake_interval_seconds <= 0 with a clear
    RuntimeError, instead of letting a "0" value spin the loop with no
    delay between iterations."""
    _stub_config(clean_env)
    clean_env.setenv("EDP_WAKE_INTERVAL_SECONDS", "0")

    with pytest.raises(RuntimeError, match="EDP_WAKE_INTERVAL_SECONDS"):
        edp_config.load_edp_config()


def test_wake_interval_negative_now_raises(clean_env):
    """Same guard rejects a negative wake interval ("-5") too."""
    _stub_config(clean_env)
    clean_env.setenv("EDP_WAKE_INTERVAL_SECONDS", "-5")

    with pytest.raises(RuntimeError, match="EDP_WAKE_INTERVAL_SECONDS"):
        edp_config.load_edp_config()


# ---------------------------------------------------------------------------
# (b) active_date_cutoff_hour out of the valid 0-23 range, via agent_config.json.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_hour", [24, -1, 100])
def test_active_date_cutoff_hour_out_of_range_now_raises(clean_env, bad_hour):
    """active_date_cutoff_hour is range-checked against 0-23; out-of-range
    values (24, -1, 100) raise RuntimeError instead of silently
    misattributing billing to the wrong trade date downstream."""
    _stub_config(clean_env, {"active_date_cutoff_hour": bad_hour})

    with pytest.raises(RuntimeError, match="active_date_cutoff_hour"):
        edp_config.load_edp_config()


def test_active_date_cutoff_hour_non_numeric_raises_uncaught_value_error(clean_env):
    """Non-numeric active_date_cutoff_hour ("six") raises ValueError
    uncaught, same as the wake-interval case — cryptic but fails loudly."""
    _stub_config(clean_env, {"active_date_cutoff_hour": "six"})

    with pytest.raises(ValueError, match="invalid literal for int"):
        edp_config.load_edp_config()


# ---------------------------------------------------------------------------
# (c) Empty-string CBOS URLs explicitly set (not unset).
# ---------------------------------------------------------------------------


def test_cbos_status_url_explicit_empty_string_no_longer_wins_over_json_default(clean_env):
    """_env_nonempty("CBOS_STATUS_URL") treats an explicitly-set empty
    string the same as unset, falling through to the agent_config.json
    value instead of letting "" silently win over every fallback."""
    _stub_config(clean_env, {"cbos_status_url": "http://json-cbos:8087"})
    clean_env.setenv("CBOS_STATUS_URL", "")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_status_url == "http://json-cbos:8087"


# ---------------------------------------------------------------------------
# (d) Malformed DATABASE_URL.
# ---------------------------------------------------------------------------


def test_database_url_missing_scheme_now_raises(clean_env):
    """_validate_database_url() rejects a resolved URL with no recognized
    PostgreSQL scheme, failing loudly at config-load time instead of deep
    inside a later asyncpg connection attempt."""
    _stub_config(clean_env)
    clean_env.setenv("DATABASE_URL", "not-a-url-at-all")

    with pytest.raises(RuntimeError, match="PostgreSQL"):
        edp_config.load_edp_config()


def test_database_url_unsupported_scheme_now_raises(clean_env):
    """Same validation catches a well-formed but unsupported-scheme URL
    (mysql://...) — "PostgreSQL-only" is enforced, not just documented."""
    _stub_config(clean_env)
    clean_env.setenv("DATABASE_URL", "mysql://user:pass@host/db")

    with pytest.raises(RuntimeError, match="PostgreSQL"):
        edp_config.load_edp_config()


# ---------------------------------------------------------------------------
# (e) default.edp section is the wrong type entirely (array / string).
# ---------------------------------------------------------------------------


def test_edp_section_as_json_array_is_ignored_and_logged(clean_env, caplog):
    """The `isinstance(edp_raw, dict)` guard also catches a JSON array
    (list is not a dict): edp_raw resets to {} and an ERROR is logged
    naming the actual type ("list")."""
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {"edp": []}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True
    assert cfg.default_segments == []
    assert any("not an object" in record.message and "list" in record.message for record in caplog.records)


def test_edp_section_as_plain_string_is_ignored_and_logged(clean_env, caplog):
    """Same isinstance guard, string case, for direct comparison with the
    array case above."""
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {"edp": "oops"}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True
    assert cfg.default_segments == []
    assert any("not an object" in record.message and "str" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# (f) default_segments / default_post_trade_processes malformed as a list of
#     strings instead of a list of dicts.
# ---------------------------------------------------------------------------


def test_build_default_workflow_json_segments_as_plain_strings_raises_attribute_error():
    """build_default_workflow_json() assumes each `segments` entry is a
    dict (calls seg.get("segment_code", "")). A realistic config typo —
    uploading ["EQ", "DR"] instead of [{"segment_code": "EQ", ...}] —
    raises AttributeError with no indication of which field/entry caused
    it; surfaces as a startup crash during agent bootstrap."""
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        build_default_workflow_json(segments=["EQ", "DR"])


def test_build_default_workflow_json_post_trade_processes_as_plain_strings_raises_attribute_error():
    """Same root cause in the post_trade_processes loop: passing
    ["COLALLOC", "MTFFT"] instead of dicts raises the same generic
    AttributeError and would crash startup."""
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        build_default_workflow_json(
            segments=[],
            post_trade_processes=["COLALLOC", "MTFFT"],
        )


def test_load_edp_config_now_validates_segment_shape(clean_env):
    """load_edp_config() validates each default_segments entry via
    _validate_process_entries(), raising a clear RuntimeError naming the
    index instead of the cryptic AttributeError from
    build_default_workflow_json() at auto-seed time."""
    _stub_config(clean_env, {"segments": ["EQ", "DR"]})

    with pytest.raises(RuntimeError, match=r"segments\[0\]"):
        edp_config.load_edp_config()


def test_load_edp_config_now_validates_post_trade_process_shape(clean_env):
    """Same validation, post_trade_processes side."""
    _stub_config(clean_env, {"post_trade_processes": ["COLALLOC", "MTFFT"]})

    with pytest.raises(RuntimeError, match=r"post_trade_processes\[0\]"):
        edp_config.load_edp_config()
