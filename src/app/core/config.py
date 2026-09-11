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
    # Multi-turn history window: total TOKENS of complete turns (newest
    # first) sent to the agent as `message_history`, counted with the chat
    # model's tokenizer (tiktoken; deterministic char/CJK heuristic fallback
    # when no encoding can be loaded). Breaking rename: replaces the retired
    # CHAT_HISTORY_CHAR_BUDGET (chars mis-measured mixed CJK/English; a
    # leftover KB_CHAT_HISTORY_CHAR_BUDGET env var is silently ignored —
    # Settings ignores unknown keys). 2000 tokens ≈ the old 8000 chars of
    # mixed CJK/English content.
    CHAT_HISTORY_TOKEN_BUDGET: int = 2000
    # Long-document guardrail: one turn may contribute at most this fraction
    # of the history token budget; an oversized turn (e.g. a long pasted
    # document) is admitted truncated-with-marker instead of silently
    # evicting all other history. >= 1.0 disables the guardrail (turns stand
    # whole; the all-or-nothing walk of the char-budget era).
    CHAT_HISTORY_MAX_TURN_FRACTION: float = 0.5
    # History-aware query rewriting for follow-ups: before each non-first
    # turn, a rewrite call turns anaphoric questions ("那它的缺点呢?") into a
    # self-contained retrieval query using recent history. false disables the
    # step entirely (byte-identical to the no-rewrite service).
    CHAT_QUERY_REWRITE_ENABLED: bool = True
    # Recent complete turns (user + assistant, so 2*N messages) fed to the
    # rewriter — a latency/cost bound smaller than the full history window;
    # <= 0 reuses the whole assembled window.
    CHAT_REWRITE_HISTORY_TURNS: int = 3
    # Rolling summary of evicted history: turns that fall outside the token
    # window are folded incrementally (per-session watermark) into a stored
    # summary that leads the next turn's history as labeled context, so older
    # context degrades gradually instead of vanishing at the window edge.
    # Maintained best-effort after each answer (one extra model call only
    # when new turns evicted). false disables the step entirely — no summary
    # column consulted, no fold call, no injection (byte-identical to
    # cliff eviction); stored summaries stay but are unused.
    CHAT_ROLLING_SUMMARY_ENABLED: bool = True
    # Bound on the rolling summary, in tokens: the fold prompt asks the model
    # to stay within it, and the same amount (capped at the summary's actual
    # cost) is reserved from CHAT_HISTORY_TOKEN_BUDGET when a summary is
    # injected, so turn growth can never evict the summary itself.
    CHAT_SUMMARY_MAX_TOKENS: int = 400
    # Carry the previous run's sources into follow-up turns: each run's
    # retrieval sources are persisted on the assistant message, and the next
    # turn re-emits them as its first `sources` batch (fresh numbering
    # continues after them) and as a labeled citable context pair leading the
    # prompt. false is the runtime kill switch — byte-identical to the
    # pre-carry service (no sources write, no first batch, no preamble);
    # stored sources are simply not carried.
    CHAT_SOURCES_CARRY_ENABLED: bool = True

    # --- MCP extension ---
    # Path to a Claude-Desktop-style `{"mcpServers": {...}}` file; relative
    # paths resolve against the working directory. Missing file = no servers.
    MCP_CONFIG_PATH: str = "mcp.json"

    # --- cache (opt-in, best-effort Redis) ---
    # Master switch for the cache layer (core/cache.py). Effective enable =
    # CACHE_ENABLED AND non-empty REDIS_URL, so the default (empty REDIS_URL)
    # stays a no-op NullCache — byte-identical to pre-cache behavior, zero
    # Redis connections. A deployment with Redis configured for the ARQ queue
    # can still turn caching off here without unsetting KB_REDIS_URL.
    CACHE_ENABLED: bool = True
    # Embedding vector cache (per text): deterministic outputs, so a long TTL
    # is safe. 0 = no expiry (the key version bump / eviction clears it).
    CACHE_EMBEDDING_TTL_S: int = 2592000  # 30 days
    # Summarize result cache: keyed on content_hash (self-invalidating on
    # edit), so no expiry is needed. 0 = no expiry.
    CACHE_SUMMARY_TTL_S: int = 0
    # Association result cache: depends on OTHER documents (a neighbor may
    # change without touching this one's hash), so a short TTL is the
    # staleness bound.
    CACHE_ASSOCIATION_TTL_S: int = 600  # 10 minutes
    # Search outcome cache: invalidated by the global search epoch (bumped on
    # every document write); the TTL is a secondary backstop.
    CACHE_SEARCH_TTL_S: int = 60

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
    # Vector rescue on-domain trigger: rescue fires ONLY when the emptied
    # leg's own minimum distance is at or below this — an off-domain leg
    # (closest hit already unrelated) stays empty instead of rescuing its
    # noise band. Calibrated 2026-09-10 on the real 15-doc corpus:
    # in-domain short-keyword leg_min 0.473-0.652, off-domain queries
    # 0.656-0.774; 0.62 is the precision-first policy choice (keeps the whole
    # in-domain recall band except bare `事务`, whose BM25 leg still fires) —
    # see task 09-10-irrelevant-query-noise-gates design.md. `2.0` (or
    # anything greater) disables the trigger: rescue then fires whenever the
    # ceiling above empties the leg (the pre-09-10 behavior).
    SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE: float = 0.62
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
