"""
load_edp_config() must not silently fall back to mock-mode/localhost
defaults without a trace in the logs, and must support a hard opt-in
failure mode (EDP_STRICT_CONFIG=true) for production deployments that want
a broken config to crash on boot rather than run "successfully" against
nothing real all day.

The database URL is stricter still: there's no soft/logged default for it
at all (no local-file fallback) — an unresolvable database_url always
raises immediately, regardless of EDP_STRICT_CONFIG, since there's no safe
value to run against instead of the real PostgreSQL instance.

These are unit tests — no database, no CBOS — load_agent_config() itself is
monkeypatched so each test controls the effective agent_config.json shape
directly, independent of whatever real agent_config.json exists on disk or
whatever DB_*/CBOS_* env vars the environment running the suite happens to
already have set (those are cleared here for isolation).
"""

from __future__ import annotations

import pytest

import src.agent.edp.config as edp_config

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
)


@pytest.fixture
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def test_missing_edp_section_and_no_db_config_raises(clean_env):
    """
    Unlike cbos_status_url/cbos_process_url/cbos_use_mock, the database has
    no safe hardcoded default to fall through to (no local-file fallback) —
    an unconfigured database always raises immediately, unconditionally,
    not gated behind EDP_STRICT_CONFIG.
    """
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})

    with pytest.raises(RuntimeError, match="EDP database is not configured"):
        edp_config.load_edp_config()


def test_missing_edp_section_defaults_other_settings_but_does_not_raise(clean_env, caplog):
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True
    assert cfg.database_url == "postgresql+asyncpg://user:pw@dbhost:5432/edp"
    assert any("falling through to hardcoded defaults" in record.message for record in caplog.records), (
        "must log a visible warning about which settings defaulted"
    )


def test_malformed_edp_section_is_ignored_and_logged(clean_env, caplog):
    """edp: "not-a-dict" (wrong type) must not crash config loading, but must
    be loudly flagged rather than silently misbehaving in some other way."""
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {"edp": "not-a-dict"}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is True
    assert any("not an object" in record.message for record in caplog.records)


def test_explicit_config_values_are_not_flagged_as_defaulted(clean_env, caplog):
    clean_env.setattr(
        edp_config,
        "load_agent_config",
        lambda: {
            "default": {
                "edp": {
                    "database_url": "postgresql+asyncpg://user:pw@dbhost:5432/edp",
                    "cbos_status_url": "http://real-cbos:8087",
                    "cbos_process_url": "http://real-cbos:8003",
                    "cbos_use_mock": False,
                }
            }
        },
    )

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is False
    assert cfg.database_url == "postgresql+asyncpg://user:pw@dbhost:5432/edp"
    assert not any("falling through to hardcoded defaults" in record.message for record in caplog.records)


def test_env_vars_are_not_flagged_as_defaulted(clean_env, caplog):
    """
    edp_raw itself is still empty here (so the separate "no default.edp
    section" notice is expected and fine) — what must NOT happen is the
    settings-level "falling through to hardcoded defaults" warning, since
    every critical setting is explicitly provided via env vars.
    """
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")
    clean_env.setenv("CBOS_STATUS_URL", "http://real-cbos:8087")
    clean_env.setenv("CBOS_PROCESS_URL", "http://real-cbos:8003")
    clean_env.setenv("CBOS_USE_MOCK", "false")

    cfg = edp_config.load_edp_config()

    assert cfg.cbos_use_mock is False
    assert not any("falling through to hardcoded defaults" in record.message for record in caplog.records)


def test_strict_config_raises_when_settings_are_unconfigured(clean_env):
    """
    Database URL is provided here so the raise under test is specifically
    the EDP_STRICT_CONFIG one for the cbos_* settings — the database's own
    unconditional raise is covered separately above.
    """
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")
    clean_env.setenv("EDP_STRICT_CONFIG", "true")

    with pytest.raises(RuntimeError, match="EDP_STRICT_CONFIG"):
        edp_config.load_edp_config()


def test_strict_config_does_not_raise_when_fully_configured(clean_env):
    clean_env.setattr(edp_config, "load_agent_config", lambda: {"default": {}})
    clean_env.setenv("EDP_STRICT_CONFIG", "true")
    clean_env.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/edp")
    clean_env.setenv("CBOS_STATUS_URL", "http://real-cbos:8087")
    clean_env.setenv("CBOS_PROCESS_URL", "http://real-cbos:8003")
    clean_env.setenv("CBOS_USE_MOCK", "false")

    cfg = edp_config.load_edp_config()  # must not raise
    assert cfg.cbos_use_mock is False
