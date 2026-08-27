"""Application settings, loaded from `KB_*` environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration; the single source of truth for `src/`."""

    model_config = SettingsConfigDict(
        env_prefix="KB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- datastore ---
    DATABASE_URL: str = "postgresql+asyncpg://kb:kb@localhost:5432/kb"
    ELASTICSEARCH_URL: str = "http://localhost:9200"

    # --- LLM provider (OpenAI-compatible) ---
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_API_KEY: SecretStr = SecretStr("")
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    # Fixed vector dimension — see database-guidelines.md; changing it is a
    # dedicated new-column + backfill migration, never a casual edit.
    EMBEDDING_DIM: int = 1536
    CHAT_MODEL: str = "gpt-4o-mini"

    # --- observability ---
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "console"  # "console" | "json"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor; tests may `cache_clear()` and re-set env."""
    return Settings()
