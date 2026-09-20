from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    slack_bot_token: str
    slack_app_token: str | None = None
    slack_signing_secret: str | None = None
    database_url: str | None = None
    redis_url: str | None = None
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    min_sample_threshold: int = 3
    max_per_bucket: int = 3
    max_suggested_candidates: int = 6
    thread_recency_days: int = 7
    include_thread_signal: bool = False
    include_affinity: bool = False
    affinity_decay_halflife_days: float = 30.0
    presence_cache_ttl_seconds: int = 45
    known_response_limit: int = 3

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
