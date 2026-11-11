"""Application settings, loaded from environment variables (and an optional `.env` file)."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Each field maps to an upper-case env var of the same name.

    Dict fields (`WEBHOOK_SECRETS`, `DESTINATIONS`) are read from the environment as JSON,
    e.g. `DESTINATIONS='{"crm": "https://example.com/hook"}'`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # `ANTHROPIC_API_KEY=` (empty) means "not set", not ""
    )

    # Storage
    database_path: str = "lead_router.db"

    # Inbound webhooks
    webhook_secrets: dict[str, str] = Field(default_factory=dict)
    signature_tolerance_seconds: int = 300
    meta_verify_token: SecretStr | None = None

    # Cleaning and dedupe
    default_region: str = "IN"
    dedupe_window_hours: int = 24

    # Scoring
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-haiku-5-5"
    llm_timeout_seconds: float = 20.0

    # Outbound delivery
    destinations: dict[str, str] = Field(default_factory=dict)
    outbound_signing_secret: SecretStr | None = None
    forward_spam: bool = False
    delivery_max_attempts: int = 6
    delivery_base_delay_seconds: float = 2.0
    delivery_max_delay_seconds: float = 300.0
    delivery_timeout_seconds: float = 10.0
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 20

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached after the first call)."""
    return Settings()
