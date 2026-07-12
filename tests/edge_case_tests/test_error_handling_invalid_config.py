"""
Error-handling / invalid-input tests for src/agent/edp/config.py.

These deliberately feed nonsensical or malformed values into
load_edp_config() / build_default_workflow_json() to find real bugs —
uncaught crashes, cryptic error messages, or silent misbehavior that would
only surface later as a confusing downstream failure (e.g. an ops person
uploading a typo'd agent_config.json and the agent either crashing with an
unhelpful traceback, or starting up "successfully" with garbage settings).

This is investigation, not regression-proofing for already-validated
behavior: tests/test_config_loading.py covers the WARNING/EDP_STRICT_CONFIG
paths, and tests/unit_tests/test_config_env_precedence.py covers the
resolution-order logic. This file only covers scenarios where the input
itself is invalid/nonsensical.

No real DB, no real agent_config.json on disk: load_agent_config() is
monkeypatched per test (same pattern as the two files above) so the JSON
side is fully controlled, and all relevant env vars are cleared before each
test for isolation.

No source changes are made based on findings here — each test documents,
via assertion + comment, whichever of the three outcomes actually happens:
(1) raises cleanly, (2) raises cryptically, or (3) is silently accepted.
"""

from __future__ import annotations

import pytest

import src.agent.edp.config as edp_config
from src.agent.edp.config import build_default_workflow_json

_ENV_KEYS = (
    "DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USERNAME", "DB_PASSWORD",
    "CBOS_STATUS_URL", "CBOS_PROCESS_URL", "CBOS_USE_MOCK", "EDP_STRICT_CONFIG",
    "CBOS_LOGIN_ID", "POST_TRADE_LOGIN_ID", "EDP_WAKE_INTERVAL_SECONDS",
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
        edp_config, "load_agent_config",
        lambda: {"default": {"edp": edp_raw or {}}},
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")


# ---------------------------------------------------------------------------
# (a) EDP_WAKE_INTERVAL_SECONDS invalid values.
# ---------------------------------------------------------------------------

def test_wake_interval_non_numeric_raises_uncaught_value_error(clean_env):
    """
    int(os.getenv("EDP_WAKE_INTERVAL_SECONDS", "60")) with a non-numeric
    string raises ValueError, and there is no try/except anywhere in
    load_edp_config() to catch it — it propagates straight out to the
    caller.

    Message clarity: CPython's ValueError message is
    "invalid literal for int() with base 10: 'not_a_number'" — it does at
    least name the bad value, but gives no hint that the offending source
    was the EDP_WAKE_INTERVAL_SECONDS env var specifically. An ops person
    seeing this bare traceback at boot would have to go read the source to
    know which env var to fix. Cryptic, not actionable on its own.
    """
    _stub_config(clean_env)
    clean_env.setenv("EDP_WAKE_INTERVAL_SECONDS", "not_a_number")

    with pytest.raises(ValueError, match="invalid literal for int"):
        edp_config.load_edp_config()


def test_wake_interval_zero_is_silently_accepted(clean_env):
    """
    "0" parses fine as an int and there is no guard anywhere against a
    zero-second wake interval. Risk: whatever scheduler/loop reads
    wake_interval_seconds would busy-loop with no delay between iterations
    (CPU spin, DB hammering) if this ever reached production. Nothing in
    config.py validates a sane minimum.
    """
    _stub_config(clean_env)
    clean_env.setenv("EDP_WAKE_INTERVAL_SECONDS", "0")

    cfg = edp_config.load_edp_config()

    assert cfg.wake_interval_seconds == 0  # nonsensical but accepted as-is


def test_wake_interval_negative_is_silently_accepted(clean_env):
    """
    "-5" also parses fine as an int. A negative wake interval is
    meaningless for a sleep/scheduling loop but nothing rejects it here —
    same busy-loop/undefined-behavior risk as the zero case, or worse if
    the caller passes this straight to something like asyncio.sleep(-5)
    (which itself raises, but only much later / deeper in the call stack,
    far from this config-loading boundary).
    """
    _stub_config(clean_env)
    clean_env.setenv("EDP_WAKE_INTERVAL_SECONDS", "-5")

    cfg = edp_config.load_edp_config()

    assert cfg.wake_interval_seconds == -5  # nonsensical but accepted as-is


# ---------------------------------------------------------------------------
# (b) active_date_cutoff_hour out of the valid 0-23 range, via agent_config.json.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_hour", [24, -1, 100])
def test_active_date_cutoff_hour_out_of_range_is_silently_accepted(clean_env, bad_hour):
    """
    int(edp_raw.get("active_date_cutoff_hour", 6)) only converts the type;
    there is no range check against the documented 0-23 valid range
    anywhere in load_edp_config(). A value of 24, -1, or 100 is accepted
    as-is. Risk: whatever "is it before/after cutoff_hour today" comparison
    consumes this downstream (likely something like
    `now.hour < cutoff_hour`) would behave nonsensically — e.g. 24 or 100
    make the "before cutoff" branch always true (since now.hour is always
    0-23 and thus always < 24/100), silently treating every timestamp of
    the day as "previous calendar day"; -1 makes it always false, treating
    every timestamp as "today" regardless of actual time. Either way,
    active-date attribution for billing would be silently wrong with no
    error raised anywhere near config load time.
    """
    _stub_config(clean_env, {"active_date_cutoff_hour": bad_hour})

    cfg = edp_config.load_edp_config()

    assert cfg.active_date_cutoff_hour == bad_hour  # nonsensical but accepted as-is


def test_active_date_cutoff_hour_non_numeric_raises_uncaught_value_error(clean_env):
    """
    int("six") raises ValueError, uncaught, same as the wake-interval case.
    Message clarity: "invalid literal for int() with base 10: 'six'" names
    the bad value but not which config key produced it — an ops person
    would need to trace back into config.py to know it was
    active_date_cutoff_hour in agent_config.json rather than an env var.
    Cryptic but at least fails loudly at boot rather than starting up with
    a bad value.
    """
    _stub_config(clean_env, {"active_date_cutoff_hour": "six"})

    with pytest.raises(ValueError, match="invalid literal for int"):
        edp_config.load_edp_config()


# ---------------------------------------------------------------------------
# (c) Empty-string CBOS URLs explicitly set (not unset).
# ---------------------------------------------------------------------------

def test_cbos_status_url_explicit_empty_string_wins_over_json_default(clean_env):
    """
    os.getenv("CBOS_STATUS_URL", edp_raw.get(...)) only falls back to its
    default argument when the env var key is entirely ABSENT from
    os.environ — an explicitly-set empty string is a present key, so
    os.getenv returns "" outright; the agent_config.json value (and the
    hardcoded "http://localhost:8087") never get a chance to apply.

    This also means cbos_status_url_defaulted's `not edp_raw.get(...)`
    half doesn't fire (edp_raw.get is non-empty here), but the falsy ""
    result isn't flagged as "defaulted" either way — the settings-level
    WARNING only fires for the *fully unset* case, not for "set to
    garbage". So an empty CBOS_STATUS_URL is accepted completely silently:
    no warning, no error. Downstream, any HTTP client built from this URL
    would fail with a confusing "no scheme supplied" / connection error
    far from this config-loading boundary, with no indication the root
    cause was an empty env var.
    """
    _stub_config(clean_env, {"cbos_status_url": "http://json-cbos:8087"})
    clean_env.setenv("CBOS_STATUS_URL", "")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_status_url == ""  # nonsensical but accepted as-is, no warning


# ---------------------------------------------------------------------------
# (d) Malformed DATABASE_URL.
# ---------------------------------------------------------------------------

def test_database_url_missing_scheme_passes_through_unvalidated(clean_env):
    """
    _normalize_postgres_url() only special-cases a "postgresql://" prefix;
    anything else (including a string with no scheme at all) is returned
    completely unchanged. There is no validation that the value even looks
    like a URL. This would only fail later, deep inside whatever SQLAlchemy
    engine/asyncpg connection call consumes database_url, with an error
    message that gives no hint the root cause was agent_config.json /
    DATABASE_URL.
    """
    _stub_config(clean_env)
    clean_env.setenv("DATABASE_URL", "not-a-url-at-all")

    cfg = edp_config.load_edp_config()

    assert cfg.database_url == "not-a-url-at-all"  # passed through unvalidated


def test_database_url_unsupported_scheme_passes_through_unvalidated(clean_env):
    """
    A well-formed but unsupported-scheme URL (mysql://...) also isn't
    special-cased by _normalize_postgres_url() (it only matches the exact
    "postgresql://" prefix) — it's returned unchanged. The module docstring
    says "PostgreSQL-only" but nothing enforces that at config-load time;
    a typo'd or copy-pasted mysql:// connection string would sail through
    and only fail once something tries to actually open the connection
    with an async Postgres driver.
    """
    _stub_config(clean_env)
    clean_env.setenv("DATABASE_URL", "mysql://user:pass@host/db")

    cfg = edp_config.load_edp_config()

    assert cfg.database_url == "mysql://user:pass@host/db"  # unsupported scheme, unvalidated


# ---------------------------------------------------------------------------
# (e) default.edp section is the wrong type entirely (array / string).
# ---------------------------------------------------------------------------

def test_edp_section_as_json_array_is_ignored_and_logged(clean_env, caplog):
    """
    `if not isinstance(edp_raw, dict)` correctly catches a JSON array too
    (list is not a dict) — edp_raw is reset to {} and a clear ERROR is
    logged naming the actual type ("list"). Confirms the isinstance guard
    generalizes beyond the string case already covered in
    test_config_loading.py's test_malformed_edp_section_is_ignored_and_logged.
    """
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {"edp": []}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True
    assert cfg.default_segments == []
    assert any(
        "not an object" in record.message and "list" in record.message
        for record in caplog.records
    )


def test_edp_section_as_plain_string_is_ignored_and_logged(clean_env, caplog):
    """Same isinstance guard, string case, spelled out explicitly here
    (the referenced existing test uses the same value; repeated for direct
    side-by-side comparison with the array case above within this file)."""
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {"edp": "oops"}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True
    assert cfg.default_segments == []
    assert any(
        "not an object" in record.message and "str" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# (f) default_segments / default_post_trade_processes malformed as a list of
#     strings instead of a list of dicts.
# ---------------------------------------------------------------------------

def test_build_default_workflow_json_segments_as_plain_strings_raises_attribute_error():
    """
    build_default_workflow_json() calls seg.get("segment_code", "") on each
    entry in `segments`, assuming each entry is a dict. A realistic config
    typo — someone uploading ["EQ", "DR"] instead of
    [{"segment_code": "EQ", ...}, ...] — passes a list of plain strings.
    str has no .get() method, so this raises AttributeError.

    Message clarity: "'str' object has no attribute 'get'" at least names
    the wrong type and the missing method, which is somewhat informative
    to a developer, but gives zero indication of *which config field* or
    *which segment entry* caused it, and would surface as a startup crash
    (this is called during agent bootstrap to auto-seed a workflow) rather
    than a clean, actionable "segments[0] must be an object with
    segment_code" validation error. For an ops person debugging a bad
    deploy, this is a cryptic crash: it says nothing about
    agent_config.json, "segments", or JSON at all.
    """
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        build_default_workflow_json(segments=["EQ", "DR"])


def test_build_default_workflow_json_post_trade_processes_as_plain_strings_raises_attribute_error():
    """
    Same bug, same root cause, in the post_trade_processes loop: proc.get(
    "process_code", "") assumes each entry is a dict. Passing
    ["COLALLOC", "MTFFT"] instead of [{"process_code": "COLALLOC"}, ...]
    raises AttributeError with the same generic, unhelpful message as the
    segments case above — equally cryptic, equally a realistic config-typo
    scenario that would crash startup rather than fail gracefully with a
    clear "malformed post_trade_processes entry" error.
    """
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        build_default_workflow_json(
            segments=[],
            post_trade_processes=["COLALLOC", "MTFFT"],
        )


def test_load_edp_config_itself_does_not_validate_segment_shape(clean_env):
    """
    load_edp_config() itself just passes edp_raw.get("segments", []) straight
    through into EdpBootstrapConfig.default_segments with no shape
    validation at all — a list of plain strings loads without error here.
    The AttributeError above only happens later, whenever
    build_default_workflow_json() is actually called with this value (e.g.
    on first run with no edpb_properties row). This means a malformed
    segments list in agent_config.json passes config loading cleanly and
    only blows up later, at auto-seed time, with the cryptic error from the
    previous two tests — a delayed, hard-to-correlate failure rather than
    an immediate one at config-load time.
    """
    _stub_config(clean_env, {"segments": ["EQ", "DR"]})

    cfg = edp_config.load_edp_config()

    assert cfg.default_segments == ["EQ", "DR"]  # malformed shape, accepted with no validation
