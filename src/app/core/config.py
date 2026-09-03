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
    ES_INDEX: str = "kb_documents"

    # --- http / CORS ---
    # Allowed CORS origins for browser clients; empty disables CORS entirely
    # (the production default). Local development: ["*"] or explicit origins,
    # e.g. ["http://localhost:5173"]. Credentials are never allowed, so the
    # "*" wildcard stays legal.
    CORS_ORIGINS: list[str] = []

    # --- LLM providers (OpenAI-compatible) ---
    # Embedding and chat are independently configurable: each layer reads only
    # its own base_url/api key, so the two may point at different providers.
    EMBEDDING_BASE_URL: str = "https://api.openai.com/v1"
    EMBEDDING_API_KEY: SecretStr = SecretStr("")
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    # Fixed vector dimension — see database-guidelines.md; changing it is a
    # dedicated new-column + backfill migration, never a casual edit.
    EMBEDDING_DIM: int = 1536
    CHAT_BASE_URL: str = "https://api.openai.com/v1"
    CHAT_API_KEY: SecretStr = SecretStr("")
    CHAT_MODEL: str = "gpt-4o-mini"
    # Multi-turn history window: total characters of complete turns (newest
    # first) sent to the agent as `message_history` — chars, not tokens, are
    # the MVP proxy (PRD out-of-scope: token-accurate budgeting).
    CHAT_HISTORY_CHAR_BUDGET: int = 8000

    # --- MCP extension ---
    # Path to a Claude-Desktop-style `{"mcpServers": {...}}` file; relative
    # paths resolve against the working directory. Missing file = no servers.
    MCP_CONFIG_PATH: str = "mcp.json"

    # --- indexing queue (ARQ + Redis) ---
    # Empty (default) = indexing runs as in-process background tasks after
    # each write; local development stays zero-dependency. Set to the compose
    # Redis (redis://localhost:6379) and run `python -m app.cli worker` to
    # route indexing through the ARQ task queue with automatic retries.
    REDIS_URL: str = ""
    # Queue-level retry policy for indexing jobs: attempts per job, and the
    # delay before the first retry (doubling each attempt: 5s, 10s, 20s ...).
    INDEX_JOB_MAX_TRIES: int = 3
    INDEX_JOB_RETRY_MIN_DELAY_S: int = 5

    # --- observability ---
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "console"  # "console" | "json"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor; tests may `cache_clear()` and re-set env."""
    return Settings()
