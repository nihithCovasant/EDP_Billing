from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration. Field names map case-insensitively to the same
    environment variable names the app has always used (FILE_ROOT_PATH,
    DATABASE_URL, etc.) - .env / .env.test files are unchanged."""

    file_root_path: str
    date_folder_format: str = "%Y-%m-%d"
    poll_interval_minutes: int = 5

    cbos_upload_url: str
    cbos_timeout_seconds: int = 30

    database_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
