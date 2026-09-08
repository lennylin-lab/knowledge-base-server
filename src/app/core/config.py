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

    # --- search relevance gates ---
    # Quality gates inside the hybrid retriever (rag/retriever.py): weak matches
    # are dropped instead of padding results — empty beats noise on small
    # corpora. Defaults mirror the Retriever constructor constants; keep the
    # two in sync (guarded by a unit test).
    # BM25 leg: drop ES hits with `_score` below this; `0.0` disables.
    # 0.0 is the deliberate default — an absolute BM25 floor cannot work
    # (measured 2026-09-08, task 09-08-es-bm25-scoring D5): the score scale
    # is query-dependent, top hits spanning 4.46 ("for 循环怎么写") to 28.39
    # ("setState 状态管理") with min/top ratios 0.045-0.586 within result
    # sets, so a floor calibrated for one query is meaningless for another.
    # The scale-free noise gate is term coverage (SEARCH_BM25_MIN_COVERAGE);
    # keep this field only as a measured operator escape hatch, never
    # re-enable it blindly.
    SEARCH_BM25_MIN_SCORE: float = 0.0
    # BM25 leg term coverage: ES `minimum_should_match` applied to the
    # `chunk_text` leaf of the BM25 query (search/queries.py) — how much of
    # the query a document must match on the prose field. Scale-free, unlike
    # the score floor above: "matched 70% of the query's terms" means the
    # same thing across queries with different BM25 score scales. Empty
    # string omits the key entirely (gate disabled). Applied to `chunk_text`
    # only: its IK tokenization defines the query's terms, while the `code`
    # subfield keeps English stopwords IK drops (a percentage would mean
    # something different there) and title/heading breadcrumbs are short.
    SEARCH_BM25_MIN_COVERAGE: str = "70%"
    # Vector leg: drop pgvector hits with cosine distance (range 0..2) above
    # this; `2.0` (or anything greater) disables.
    SEARCH_VECTOR_MAX_DISTANCE: float = 0.45
    # Vector leg head rescue: ONLY when the ceiling above empties the leg,
    # rows within `min(leg_min + margin, rescue_max_distance)` are admitted to
    # fusion. Short keyword queries sit systematically farther from long
    # chunks than long natural-language queries, so the absolute ceiling can
    # silence the whole leg; the rescue window (anchored to the leg's own
    # minimum) restores that recall while the cap keeps a hard noise floor —
    # a leg whose minimum distance exceeds it rescues nothing. Either value
    # `<= 0` disables rescue entirely (exact single-tier behavior).
    SEARCH_VECTOR_RESCUE_MARGIN: float = 0.15
    SEARCH_VECTOR_RESCUE_MAX_DISTANCE: float = 0.85
    # Post-fusion: keep a hit only when its fused score is at least this
    # fraction of the top hit's score; `0.0` disables.
    SEARCH_RRF_MIN_RELATIVE: float = 0.35
    # Query length cap: `retrieve()` truncates over-long queries to this many
    # characters before any leg runs — the single enforcement point for the
    # API and the agent tools (standard-analyzer CJK yields ~1 token per
    # char, so a multi-thousand-char query overflows Lucene's clause limit
    # and 502s the ES leg). `<= 0` disables.
    SEARCH_MAX_QUERY_LENGTH: int = 256

    # --- observability ---
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "console"  # "console" | "json"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor; tests may `cache_clear()` and re-set env."""
    return Settings()
