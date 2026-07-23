"""
Application settings from environment variables.
Customize this file to add your own configuration options.
"""

from pydantic_settings import BaseSettings


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
    app_config_path: str | None = None  # External config path for multi-instance support

    # A2A capabilities
    streaming_enabled: bool = True

    # LLM API keys - ONLY API keys come from environment/secret manager
    # All other secrets (Langfuse, Sentry, DB, LiteLLM) are in config.json
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None

    # Tool API keys - add more as needed (these come from secret manager)
    tavily_api_key: str | None = None
    serp_api_key: str | None = None
    gcp_service_account_json: str | None = None  # Path to service account JSON file
    pinecone_api_key: str | None = None

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
