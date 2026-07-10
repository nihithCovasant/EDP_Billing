"""
Application settings.

Defaults for server/agent fields are read from agent_config.json (the single
source of truth for this deployment — see agent_config.server /
agent_definition) rather than a .env file. Real process environment
variables still override these (useful for CI/K8s), but no local .env is
read anymore.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic_settings import BaseSettings


def _load_config_defaults() -> Dict[str, Any]:
    """Read agent_config.json's server + agent_definition sections for
    Settings field defaults. Falls back to hardcoded values below if the
    file is missing or malformed — never raises."""
    try:
        config_path = Path(__file__).parent / "agent_config.json"
        with open(config_path) as f:
            data = json.load(f)
        server = data.get("agent_config", {}).get("server", {})
        agent_def = data.get("agent_definition", {})
        return {
            "host": server.get("host"),
            "port": server.get("port"),
            "log_level": server.get("log_level"),
            "streaming_enabled": server.get("streaming_enabled"),
            "agent_name": agent_def.get("name"),
            "agent_description": agent_def.get("description"),
        }
    except Exception:
        return {}


_defaults = _load_config_defaults()


class Settings(BaseSettings):
    """
    Application settings. Server/agent fields default from agent_config.json;
    everything else (API keys, feature flags) still comes from the process
    environment (secret manager injected env vars in real deployments).

    CUSTOMIZE THIS: Add your own settings as needed.
    """

    # Server configuration
    host: str = _defaults.get("host") or "0.0.0.0"
    port: int = _defaults.get("port") or 9999
    log_level: str = _defaults.get("log_level") or "INFO"

    # Agent configuration
    agent_name: str = _defaults.get("agent_name") or "LangGraph Agent"
    agent_description: str = _defaults.get("agent_description") or "A customizable LangGraph-based AI agent"
    app_config_path: Optional[str] = None  # External config path for multi-instance support

    # A2A capabilities
    streaming_enabled: bool = (
        _defaults.get("streaming_enabled")
        if _defaults.get("streaming_enabled") is not None
        else True
    )

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

    # Rate limiting (optional) - src.middleware.rate_limiting isn't implemented
    # in this build; defaults to off so the flag doesn't advertise a feature
    # that isn't there. Flip to True once that module exists.
    rate_limit_enabled: bool = False
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    rate_limit_burst_size: int = 10

    # Metrics (optional) - src.utils.metrics isn't implemented in this build;
    # same reasoning as rate_limit_enabled above.
    metrics_enabled: bool = False

    # Multi-tenant support (False when feature not selected, True when selected via MULTI_TENANT_ENABLED=true)
    multi_tenant_enabled: bool = False

    @property
    def agent_url(self) -> str:
        """Get the full agent URL."""
        return f"http://{self.host}:{self.port}"

    class Config:
        case_sensitive = False
        extra = "ignore"  # Allow extra environment variables


# Global settings instance
settings = Settings()
