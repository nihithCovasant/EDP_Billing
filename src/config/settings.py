"""
Application settings.

Configuration comes from agent_config.json — its `agent_config.env` block is
bridged into os.environ by apply_config_env() below, before Settings() reads it,
so the whole app is config-driven and needs no .env file.
"""

import json
import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings

_env_applied = False


def _deep_fill_missing(primary: dict, fallback: dict) -> dict:
    """Recursively fill any key in `primary` that is absent, None, or "" with
    the corresponding value from `fallback`. Never touches non-empty values,
    booleans, numbers, or already-present lists — only fills true gaps."""
    for key, fallback_val in fallback.items():
        if key not in primary or primary[key] is None or primary[key] == "":
            primary[key] = fallback_val
        elif isinstance(primary[key], dict) and isinstance(fallback_val, dict):
            _deep_fill_missing(primary[key], fallback_val)
    return primary


def load_effective_config_dict() -> dict:
    """Load agent_config.json — the CAMS-mounted one if APP_CONFIG_PATH points
    at an existing file, else the one committed/baked into the image — then,
    only if a local.agent_config.json exists at the repo root, fill in any
    blank/missing fields from it.

    local.agent_config.json is a gitignored, local-dev-only file: it never
    ships inside a built image or gets mounted by CAMS, so this fallback is a
    no-op in any real deployment — there, agent_config.json alone always wins.
    """
    ext = os.getenv("APP_CONFIG_PATH")
    primary_path = (
        Path(ext)
        if ext and Path(ext).exists()
        else Path(__file__).parent / "agent_config.json"
    )
    with open(primary_path) as f:
        primary = json.load(f)

    local_path = Path(__file__).resolve().parents[2] / "local.agent_config.json"
    if local_path.exists():
        with open(local_path) as f:
            local = json.load(f)
        primary = _deep_fill_missing(primary, local)

    return primary


def apply_config_env() -> None:
    """Bridge the effective agent_config.json's `agent_config.env` block into
    os.environ so every env-reading consumer (this Settings object, the EDP
    config loader, cams_otel_lib, global_email_service, ...) is fed from that
    single source — no .env required.

    Defined here rather than in a dedicated module because Settings() must call
    it before reading the environment, and settings.py imports nothing else from
    `src` — so keeping it here avoids the circular import a separate config
    module would cause. Uses os.environ.setdefault(), so an explicitly-set real
    environment variable still overrides the config value. Idempotent; never
    raises (a missing/malformed config just leaves os.environ as-is).
    """
    global _env_applied
    if _env_applied:
        return
    _env_applied = True
    try:
        data = load_effective_config_dict()
        env_block = data.get("agent_config", {}).get("env", {})
        if isinstance(env_block, dict):
            for key, value in env_block.items():
                if value is not None:
                    os.environ.setdefault(str(key), str(value))
    except Exception:
        pass


# Populate os.environ from agent_config.json before Settings() reads it.
apply_config_env()


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    CUSTOMIZE THIS: Add your own settings as needed.
    """

    # Server configuration
    host: str = "0.0.0.0"
    port: int = 9999
    log_level: str = "INFO"

    # Agent configuration
    agent_name: str = "LangGraph Agent"
    agent_description: str = "A customizable LangGraph-based AI agent"
    app_config_path: Optional[str] = None  # External config path for multi-instance support

    # A2A capabilities
    streaming_enabled: bool = True

    # LLM API keys - ONLY API keys come from environment/secret manager
    # All other secrets (Langfuse, Sentry, DB, LiteLLM) are in config.json
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None

    # Tool API keys - add more as needed (these come from secret manager)
    tavily_api_key: Optional[str] = None
    serp_api_key: Optional[str] = None
    gcp_service_account_json: Optional[str] = None  # Path to service account JSON file
    pinecone_api_key: Optional[str] = None

    # PostgreSQL connection string (set when postgresql feature is selected)

    # Rate limiting (optional) - can be overridden in config
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    rate_limit_burst_size: int = 10

    # Metrics (optional) - can be overridden in config
    metrics_enabled: bool = True

    # Multi-tenant support (False when feature not selected, True when selected via MULTI_TENANT_ENABLED=true)
    multi_tenant_enabled: bool = False

    @property
    def agent_url(self) -> str:
        """Get the full agent URL."""
        return f"http://{self.host}:{self.port}"

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # Allow extra environment variables


# Global settings instance
settings = Settings()
