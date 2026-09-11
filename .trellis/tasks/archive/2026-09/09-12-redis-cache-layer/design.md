# Design: Redis Cache Layer

## Dependency

`uv add redis` — confirmed at review: redis-py **5.3.1** is already in
`uv.lock` (transitive via `arq`), but `pyproject.toml` lists only
`arq>=0.28.0`, so promote redis to a **direct** dep to declare the import.
Verify the installed 5.x asyncio surface (`redis.asyncio.from_url`,
`get`, `set(..., ex=ttl)`, `incr`, `aclose`) before coding — flag
deviations, same rule as the arq task.

Confirmed at review (no new plumbing needed):
- `live_redis` pytest marker already exists (added by the arq task,
  `pyproject.toml` `addopts` deselects it) — Q5 reuses it; broaden its
  one-line description from "ARQ queue round trip" to also cover cache.
- `main.py::app_lifespan` already closes shared components in a
  `try/finally` (`close_arq_pool()`); `close_cache()` slots into the same
  `finally`.

## Layering decision (why `core/cache.py`)

The cache is consumed by three layers: `llm/` (embeddings), `services/`
(agents), and `rag/` (retriever). The layering matrix
(directory-structure.md) lets **only** `core/` be imported by all three
(`llm/` may import *only* `core/`). So the primitive lives in
`core/cache.py`, cross-cutting infra exactly like `core/database.py`
(SQLAlchemy engine) and `core/logging.py`.

`redis` (redis-py) is imported **only** in `core/cache.py` — the
domain-confined-infra-import convention (`arq` in `rag/worker.py`,
provider SDKs in `llm/`). Add one line to directory-structure.md's
layering notes recording this.

## R1 — Cache primitive

```python
# core/cache.py
class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None: ...
    async def incr(self, key: str) -> int: ...          # for the search epoch
    async def aclose(self) -> None: ...

class NullCache:
    """Disabled-mode no-op: every get misses, every set drops, incr counts
    in-process only (search epoch never needed when caching is off)."""

class RedisCache:
    """redis-py asyncio wrapper. EVERY method is wrapped in try/except:
    on any redis.RedisError / OSError, warn `cache_error`(domain via the
    caller's key prefix parse, error_class only) and degrade — get→None,
    set→drop, incr→a sentinel that forces a miss. Never raises."""
```

- Values are raw `bytes`; each domain owns its own (de)serialization
  (keeps `core/` free of domain schemas).
- `ttl_seconds=0` ⇒ `SET` without expiry (persist until version bump /
  eviction).
- Best-effort wrapping is the single most important property — a unit
  test injects a fake redis client whose ops raise and asserts the
  wrapper returns None / drops without propagating.

### Config (`core/config.py`)

```
KB_CACHE_ENABLED: bool = True          # master switch (Redis may exist for ARQ yet caching be off)
KB_CACHE_EMBEDDING_TTL_S: int  = 2592000   # 30d; 0 = no expiry
KB_CACHE_SUMMARY_TTL_S: int    = 0         # rely on content_hash key; 0 = no expiry
KB_CACHE_ASSOCIATION_TTL_S: int= 600       # 10 min (cross-doc staleness)
KB_CACHE_SEARCH_TTL_S: int     = 60        # backstop under epoch invalidation
```

Effective enable = `CACHE_ENABLED and bool(REDIS_URL)`. `.env.example`
gains all five with the empty/false = today comment.

### Wiring (`api/deps.py`)

- `get_cache()` — process-lifetime accessor mirroring
  `_get_shared_arq_pool` / `get_shared_es_client`: builds one
  `RedisCache` (lazy `redis.asyncio.from_url`) when effective-enabled,
  else returns the shared `NullCache`. `close_cache()` added to the app
  lifespan next to `close_arq_pool()`.
- Injected into the existing builders:
  - `embedding_provider_from_settings` → wrap the real provider in
    `CachingEmbeddingProvider(inner, cache, ...)` when enabled (a `None`
    provider — no embedding key — stays `None`, unwrapped).
  - `build_summarize_service` / `build_association_service` → pass the
    cache in.
  - `get_document_service` / search service → pass the cache in for the
    search-epoch bump and read.
- The `lru_cache` process-lifetime accessors (`get_search_service`,
  `get_summarize_service`, …) keep memoizing the cache-injected service;
  the cache handle itself is process-lifetime, so no per-request client.

## R2 — Embedding cache

```python
# llm/embeddings.py
class CachingEmbeddingProvider:                # implements EmbeddingProvider
    def __init__(self, inner, cache, *, model, dim, ttl_seconds): ...
    async def embed_texts(self, texts):
        # 1. key each text: kb:c1:emb:{model}:{dim}:{sha256(text)}
        # 2. MGET-style per-key get; collect misses with their positions
        # 3. inner.embed_texts(misses) only if misses
        # 4. set each fresh vector; reassemble full list in input order
```

- Serialization: **`array('d').tobytes()` (float64, 12 KB/vector)** — NOT
  `array('f')`/float32. The provider returns Python floats (float64); a
  cache hit MUST return the exact same vector a miss would, or the same
  query yields different pgvector distances depending on cache state and
  the cache would change search ordering (violates the correctness rule).
  float64 round-trips the provider output bit-exactly. (JSON of the floats
  is an acceptable debuggable alternative — also exact; keep either behind
  the domain's own encode/decode helpers.)
- Empty `texts` ⇒ `[]` (unchanged). Provider `None` case never reaches
  here (wrapping is skipped in deps).
- The wrapper is pure plumbing; retries/timeouts stay in the inner
  provider's SDK client (llm spec: one place only).

## R3 — Agent result cache

- `SummarizeService` / `AssociationService` take an optional `cache`
  (default `None` ⇒ today's behavior, so the service stays unit-testable
  without Redis).
- **Load the document BEFORE the cache lookup** (both services already do
  `_load_document` / `_gather` first). This is load-order-critical, not
  incidental: (a) the key needs the freshly-read `content_hash`, and (b) a
  missing / soft-deleted document must still raise `NotFoundError` (404) —
  serving a cached summary for a now-deleted document would break the
  not-found contract. The cheap PG read is the correctness gate; the cache
  only ever short-circuits the expensive LLM run, never the existence check.
- Summarize flow: after `_load_document` (need `content_hash` + `model`),
  build key `kb:c1:summary:{doc_id}:{content_hash}:{model}`; `get` →
  on hit deserialize `SummaryResult` (recompute `latency_ms`, emit
  `cache_hit`), return; on miss run as today, then `set` the
  `model_dump_json()` bytes with `SUMMARY_TTL_S`.
- Association flow: identical shape with the assoc key + `ASSOCIATION_TTL_S`.
  Cache the **final** joined `AssociationsResult` (post model + join-back).
- `content_hash` is nullable pre-backfill; a `None` hash ⇒ skip caching
  (treat as always-miss) rather than keying on `None` — a pre-backfill
  document simply isn't cached.
- Never cache an error; only successful results are stored.

## R4 — Search cache

- Cache layer sits in `Retriever.retrieve` (the single choke point for
  API + agent tools) — or a thin wrapper the retriever calls. The
  retriever gains an optional `cache`; `None` ⇒ today's behavior.
- Epoch: `kb:c1:search:epoch` integer in Redis.
  - Read once at the top of `retrieve` (a `get`; absent ⇒ treat as `0`).
  - `DocumentService.create/update/delete` call `cache.incr(epoch_key)`
    **after commit** (a write that rolled back must not bust the cache),
    best-effort — an incr failure just means one stale-window, never a
    failed write. This is a new dependency for `DocumentService`
    (injected callable or the cache handle, framework-free like the
    `ReindexEnqueuer` seam).
- Key: `kb:c1:search:{epoch}:{sha256(truncated_query)}:{limit}:{tag|-}`
  built **after** `truncate_query` so the cache key matches what the legs
  would see.
- Value: `SearchOutcome` serialized. It is a frozen dataclass of frozen
  dataclasses (`RetrievedChunk`) with UUIDs/enums — **decision:** add a
  small `to_json`/`from_json` on the outcome (or a pydantic mirror) in
  `rag/`; do not leak dataclass pickling. Round-trip test pins field
  equality including `es_rank/vector_rank/es_score/vector_distance`.
- On hit: emit `cache_hit`(domain=search), skip both legs; on miss:
  compute as today, `set` with `SEARCH_TTL_S`, emit `cache_miss`.
- Correctness: because the epoch is in the key and every doc mutation
  bumps it, a cached outcome can only be served while the corpus is
  unchanged; the TTL is a secondary backstop. Soft-deleted content can
  never be served stale across a delete (delete bumps epoch).
- Epoch race is safe by construction: if a write bumps the epoch *during* a
  compute, the result is `set` under the now-stale epoch and simply never
  read again (subsequent reads use the new epoch → miss → recompute). Worst
  case is one wasted cache entry, never a stale read. No locking needed.

## Logging

| Event | Level | Fields |
|---|---|---|
| `cache_hit` | info | domain, (doc_id where relevant) |
| `cache_miss` | info | domain |
| `cache_error` | warning | domain, op (get/set/incr), error_class |

No key contents, no query text, no document/summary content — same
discipline as `search_executed` (q_length only) and the agent run events.

## Tradeoffs / Rejected

- **Per-key search invalidation** (rejected v1): tracking which cached
  queries a given document affects is complex and error-prone; the global
  epoch bump is coarse (any write clears all search cache) but always
  correct and O(1). Write-light single-user MVP makes the coarse cost
  negligible; revisit if writes become frequent.
- **Caching DB reads** (rejected, PRD out-of-scope): PG is fast; keyset
  pagination + soft-delete + `updated_at` touch make invalidation costly
  vs the gain.
- **Stampede / single-flight locking** (rejected v1): concurrent misses
  may double-compute; acceptable for MVP volume. Note as a future knob.
- **Caching whole chat/writing streams** (rejected): they persist per
  turn and stream token-by-token; only their embedding sub-calls cache.
- **msgpack for all values** (rejected): stdlib `array`/`json` avoid a
  new dep; embeddings use `array('f')`, agent/search use JSON.
- **A `cache/` top-level layer** (rejected): `core/cache.py` reuses the
  established cross-cutting-infra home (`core/database.py`) and satisfies
  the "importable by llm+services+rag" constraint without a new layer.

## Rollback

Set `KB_CACHE_ENABLED=false` (or unset `KB_REDIS_URL`) ⇒ `NullCache`
everywhere, byte-identical to pre-task behavior — the runtime kill
switch. Full code rollback is a single revert; no schema changes, Redis
data is ephemeral (flush the `kb:c1:*` keyspace). Bumping the `c1` key
version is the data-only invalidation if a value format ever changes.
