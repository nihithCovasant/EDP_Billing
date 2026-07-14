"""
Application settings.

Configuration comes from agent_config.json — its `agent_config.env` block is
bridged into os.environ by apply_config_env() below, before Settings() reads it,
so the whole app is config-driven and needs no .env file.
"""

import os
from typing import Optional

from pydantic_settings import BaseSettings

from src.config.config_file import load_raw_config

_env_applied = False


def apply_config_env() -> None:
    """Bridge agent_config.json's `agent_config.env` block into os.environ so
    every env-reading consumer (this Settings object, the EDP config loader,
    cams_otel_lib, global_email_service, ...) is fed from that single file — no
    .env required.

    Defined here rather than in a dedicated module because Settings() must call
    it before reading the environment, and settings.py imports nothing else from
    `src` — so keeping it here avoids the circular import a separate config
    module would cause. Uses os.environ.setdefault(), so an explicitly-set real
    environment variable still overrides the config value. Idempotent; never
    raises (a missing/malformed config just leaves os.environ as-is).

    APP_CONFIG_PATH (if set and existing) takes priority for locating the file.
    """
    global _env_applied
    if _env_applied:
        return
    _env_applied = True
    try:
        data = load_raw_config()
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
