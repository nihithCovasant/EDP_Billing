"""
Pure unit tests for the env-var / agent_config.json resolution logic in
src/agent/edp/config.py — specifically the truthy-parsing helper
(_is_truthy_env) and the env-var > agent_config.json > hardcoded-default
precedence chain for CBOS_USE_MOCK, CBOS_STATUS_URL, CBOS_PROCESS_URL, and
EDP_STRICT_CONFIG.

These do not duplicate tests/test_config_loading.py (which covers the
WARNING-on-defaulted-settings behavior and the EDP_STRICT_CONFIG
RuntimeError path end-to-end). This file only pins down the resolution
logic itself: which value wins when, and that the truthy parser accepts/
rejects the documented set of strings.

No real DB connections and no real agent_config.json on disk is relied
upon for behavior under test: load_agent_config() is monkeypatched per
test (as in test_config_loading.py) so JSON-side values are fully
controlled, and all relevant env vars are cleared before each test for
isolation. load_edp_config() has no caching (no lru_cache / module-level
singleton) — it re-reads load_agent_config() and os.environ fresh on
every call, so no cache-busting is required between tests.
"""

from __future__ import annotations

import os

import pytest

import src.agent.edp.config as edp_config
from src.agent.edp.config import EdpBootstrapConfig, _is_truthy_env

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


# ---------------------------------------------------------------------------
# (a) _is_truthy_env() recognizes the documented truthy/falsy vocabulary,
#     case-insensitively.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
def test_is_truthy_env_recognizes_truthy_values(value):
    assert _is_truthy_env(value) is True


@pytest.mark.parametrize("value", ["0", "false", "", "no", "random"])
def test_is_truthy_env_recognizes_falsy_values(value):
    assert _is_truthy_env(value) is False


def test_is_truthy_env_none_is_falsy():
    """os.getenv() returns None for an unset var; the helper must handle
    that without raising."""
    assert _is_truthy_env(None) is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
def test_cbos_use_mock_indirect_truthy_via_env(clean_env, value):
    """Same truthy vocabulary, exercised indirectly through CBOS_USE_MOCK
    resolution end-to-end (database_url supplied so only cbos_use_mock is
    under test)."""
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")
    clean_env.setenv("CBOS_USE_MOCK", value)

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True


@pytest.mark.parametrize("value", ["0", "false", "no", "random"])
def test_cbos_use_mock_indirect_falsy_via_env(clean_env, value):
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")
    clean_env.setenv("CBOS_USE_MOCK", value)

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is False


# ---------------------------------------------------------------------------
# (b) CBOS_USE_MOCK precedence: env var (if set at all, even to a falsy
#     string) wins outright over agent_config.json; only when the env var
#     is entirely unset does agent_config.json's value apply; and only when
#     neither is present does the hardcoded default (True) apply.
# ---------------------------------------------------------------------------

def test_cbos_use_mock_explicit_false_env_wins_over_json_true(clean_env):
    clean_env.setattr(edp_config, "load_agent_config", lambda: {
        "default": {"edp": {"cbos_use_mock": True}}
    })
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")
    clean_env.setenv("CBOS_USE_MOCK", "false")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is False


def test_cbos_use_mock_explicit_true_env_wins_over_json_false(clean_env):
    clean_env.setattr(edp_config, "load_agent_config", lambda: {
        "default": {"edp": {"cbos_use_mock": False}}
    })
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")
    clean_env.setenv("CBOS_USE_MOCK", "true")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True


def test_cbos_use_mock_env_unset_falls_through_to_json_value(clean_env):
    """CBOS_USE_MOCK entirely unset (not even an empty string) -> falls
    through to agent_config.json's edp.cbos_use_mock, per the source's
    `cbos_use_mock_raw is None` branch."""
    clean_env.setattr(edp_config, "load_agent_config", lambda: {
        "default": {"edp": {"cbos_use_mock": False}}
    })
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is False


def test_cbos_use_mock_env_and_json_both_unset_falls_to_hardcoded_default_true(clean_env):
    """Neither the CBOS_USE_MOCK env var nor agent_config.json's
    edp.cbos_use_mock is present -> hardcoded default True
    (EdpBootstrapConfig.cbos_use_mock's dataclass default)."""
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True


# ---------------------------------------------------------------------------
# (c) CBOS_STATUS_URL / CBOS_PROCESS_URL: explicit env values override the
#     hardcoded defaults http://localhost:8087 / http://localhost:8003.
# ---------------------------------------------------------------------------

def test_cbos_status_and_process_url_hardcoded_defaults(clean_env):
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_status_url == "http://localhost:8087"
    assert cfg.cbos_process_url == "http://localhost:8003"


def test_cbos_status_and_process_url_env_override_defaults(clean_env):
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")
    clean_env.setenv("CBOS_STATUS_URL", "http://real-cbos:9001")
    clean_env.setenv("CBOS_PROCESS_URL", "http://real-cbos:9002")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_status_url == "http://real-cbos:9001"
    assert cfg.cbos_process_url == "http://real-cbos:9002"


def test_cbos_status_and_process_url_json_value_used_when_env_unset(clean_env):
    clean_env.setattr(edp_config, "load_agent_config", lambda: {
        "default": {"edp": {
            "cbos_status_url": "http://json-cbos:8087",
            "cbos_process_url": "http://json-cbos:8003",
        }}
    })
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_status_url == "http://json-cbos:8087"
    assert cfg.cbos_process_url == "http://json-cbos:8003"


# ---------------------------------------------------------------------------
# (d) EDP_STRICT_CONFIG truthy-parsing uses the same _is_truthy_env() logic
#     as CBOS_USE_MOCK — mixed-case "YES" must resolve as truthy. We check
#     this via the helper and via the exact call-site expression (the
#     RuntimeError-raising behavior itself is covered by
#     test_config_loading.py already, so it is not re-tested here).
# ---------------------------------------------------------------------------

def test_edp_strict_config_mixed_case_yes_is_truthy_via_helper():
    assert _is_truthy_env("YES") is True


def test_edp_strict_config_mixed_case_yes_is_truthy_via_env_lookup(clean_env):
    """Confirms the actual call-site expression
    (_is_truthy_env(os.getenv("EDP_STRICT_CONFIG"))) resolves strict mode
    as active for mixed-case 'YES'."""
    clean_env.setenv("EDP_STRICT_CONFIG", "YES")
    assert _is_truthy_env(os.getenv("EDP_STRICT_CONFIG")) is True


# ---------------------------------------------------------------------------
# (e) EdpBootstrapConfig is a plain @dataclass, not a pydantic model.
# ---------------------------------------------------------------------------

def test_edp_bootstrap_config_is_plain_dataclass_not_pydantic():
    import dataclasses

    assert dataclasses.is_dataclass(EdpBootstrapConfig)
    assert not hasattr(EdpBootstrapConfig, "model_fields")

    instance = EdpBootstrapConfig(cbos_login_id="X0001")
    assert not hasattr(instance, "dict")
    # dataclasses.fields() works on a plain dataclass instance.
    field_names = {f.name for f in dataclasses.fields(instance)}
    assert "wake_interval_seconds" in field_names
    assert "cbos_use_mock" in field_names


def test_edp_bootstrap_config_construct_with_partial_fields_spot_check_defaults():
    cfg = EdpBootstrapConfig(agent_instance_id="agent-42")

    assert cfg.agent_instance_id == "agent-42"
    # Spot-check defaults for fields not explicitly set.
    assert cfg.wake_interval_seconds == 60
    assert cfg.timezone == "Asia/Kolkata"
    assert cfg.cbos_status_url == "http://localhost:8087"
    assert cfg.cbos_process_url == "http://localhost:8003"
    assert cfg.cbos_use_mock is True
    assert cfg.cbos_login_id == "CV0001"
    assert cfg.default_segments == []
    assert cfg.default_post_trade_processes == []
