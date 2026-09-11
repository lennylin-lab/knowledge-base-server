# Redis Cache Layer (embeddings, agent results, search)

## Goal

Add an **opt-in, best-effort** Redis cache that removes the most
expensive repeated work in the knowledge base: external embedding API
calls, LLM summarize/association runs, and hybrid retrieval. Caching is
a pure performance/cost optimization — it must never change a response's
correctness and must never fail a request when Redis is down.

The order below is deliberate and encodes the recommended rollout: a
shared cache primitive first, then the three cache domains in
descending value / ascending risk order (embeddings → agent results →
search). Each domain is independently shippable and independently
verifiable, so the task may stop after any domain with a coherent,
green result.

Reuses the existing `KB_REDIS_URL` (today only feeding the ARQ indexing
queue) and the ARQ dual-mode philosophy: **empty `KB_REDIS_URL` ⇒ a
no-op cache, local dev stays zero-dependency and byte-identical to
today.**

## Requirements

### R1 — Shared cache primitive (foundation)

1. A small async cache abstraction in `core/cache.py` (the only layer
   `llm/`, `services/`, and `rag/` may all import — mirrors
   `core/database.py` as cross-cutting infra):
   - a `Cache` Protocol (`get(key) -> bytes | None`,
     `set(key, value, *, ttl_seconds)`), 
   - a `RedisCache` implementation over redis-py asyncio (already a
     transitive dep via `arq`; pin it as a direct dep),
   - a `NullCache` no-op used whenever caching is disabled.
2. **Best-effort contract (binding):** every `get`/`set` on `RedisCache`
   catches all Redis/connection errors, logs a `cache_error` warning
   (error class only, no key contents), and degrades — `get` returns
   `None` (miss ⇒ recompute), `set` silently drops. A cache fault can
   never turn a working request into a 5xx or a broken stream. This
   mirrors the search-degradation and `index_enqueue_failed`
   philosophy already in the codebase.
3. **Disabled ⇒ no-op:** empty `KB_REDIS_URL` (the default) wires a
   `NullCache` everywhere; no redis client is ever constructed and
   behavior is byte-identical to today. A new `KB_CACHE_ENABLED: bool =
   True` gate lets a deployment with Redis configured (for ARQ) still
   turn caching off without unsetting `KB_REDIS_URL`.
4. Shared process-lifetime client, built lazily and closed on app
   shutdown — the `_get_shared_arq_pool` / `get_shared_es_client`
   pattern, wired once in `api/deps.py`.
5. **Versioned key namespace:** all keys start `kb:c1:<domain>:…` so the
   whole cache can be invalidated by bumping the `c1` schema version
   (e.g. when a value's serialization changes).
6. Observability: `cache_hit` / `cache_miss` events carry `domain` and
   counts only — **never** query text, document content, or key
   contents (logging spec: query/content text never logged).

### R2 — Query & document embedding cache (highest value)

1. A `CachingEmbeddingProvider` in `llm/embeddings.py` implementing the
   existing `EmbeddingProvider` Protocol by wrapping another provider +
   a `Cache`. It is the single choke point, so it covers **both** search
   query embedding and indexing-time chunk embedding.
2. **Per-text** caching within a batch: `embed_texts` checks the cache
   per input text, calls the wrapped provider only for the misses, and
   reassembles vectors in input order. A fully-cached batch makes no
   provider call.
3. Key: `kb:c1:emb:{model}:{dim}:{sha256(text)}`. Value: the vector.
   Model and dim are in the key because changing either changes the
   output (provider-config isolation lets embedding model differ from
   chat model).
4. Deterministic ⇒ long TTL. `KB_CACHE_EMBEDDING_TTL_S: int = 2592000`
   (30 days; `0` ⇒ no expiry).
5. Caching sits at the provider layer, i.e. after the retriever's
   `truncate_query` (the retriever passes already-truncated text to
   `embed_texts`), so query and reindex paths share entries.

### R3 — Summarize & association result cache

1. Cache the computed result of `SummarizeService.summarize_document`
   and `AssociationService.associate_document` (today explicitly
   "Computes (never persists)" — the most expensive operations, rerun
   every call).
2. Summarize key: `kb:c1:summary:{doc_id}:{content_hash}:{model}` —
   **self-invalidating**: the service already loads the `Document`
   carrying `content_hash`; editing content changes the hash, so a stale
   summary can never be served, no active deletion needed.
3. Association key: `kb:c1:assoc:{doc_id}:{content_hash}:{model}` with a
   **short TTL** — association depends on *other* documents (tag overlap
   + pgvector neighbors), so a new/edited/deleted neighbor makes it
   stale in a way the source hash cannot capture.
   `KB_CACHE_ASSOCIATION_TTL_S: int = 600` (10 min).
   `KB_CACHE_SUMMARY_TTL_S: int = 0` (rely on hash invalidation; 0 ⇒
   no expiry).
4. Value: the `SummaryResult` / `AssociationsResult` payload serialized
   via `model_dump_json`. On a cache hit, `latency_ms` is recomputed to
   reflect the (cheap) cache path — the stored payload is otherwise
   returned verbatim. The `agent_run_started/finished` lifecycle events
   are **not** emitted on a hit; a `cache_hit` event stands in.
5. No-candidates association (already skips the model) is still cheap;
   it may be cached or skipped — either is acceptable, keep it simple.

### R4 — Hybrid search result cache

1. Cache the `SearchOutcome` of `Retriever.retrieve` (covers the search
   API and, since it is the shared choke point, agent retrieval tools).
2. Key includes everything that changes the result:
   `kb:c1:search:{epoch}:{sha256(truncated_query)}:{limit}:{tag|-}`.
   `epoch` is a global monotonic counter (a Redis key `kb:c1:search:epoch`)
   **bumped on every document create / update / delete** in
   `DocumentService`, so any write busts the whole search cache — coarse
   but always correct, and cheap for a single-user, write-light MVP. A
   short TTL backstops it: `KB_CACHE_SEARCH_TTL_S: int = 60`.
3. Cached outcomes carry hydrated document content; the epoch bump is
   what guarantees a soft-deleted or edited document never surfaces from
   cache (the retriever already re-checks live PG on the compute path).
4. Query text is hashed into the key and **never** logged (the
   `search_executed` event already logs `q_length` only — unchanged).

## Constraints

- Best-effort only; correctness never depends on the cache.
- Offline default suite runs with **zero** Redis connections (fakes /
  `NullCache`), mirroring the ARQ offline-first test rule; any real-Redis
  test sits behind a deselected-by-default marker.
- No new database tables or migrations.
- `.env.example` documents every new `KB_CACHE_*` setting (completeness
  rule).
- Layering: the redis-py import is confined to `core/cache.py` (a spec
  note, like the `arq`-in-`rag/worker.py` note).

## Out of Scope

- Caching chat/writing **streamed** responses (they persist per turn and
  stream; only their internal embedding calls are cached via R2).
- Caching plain DB reads (documents/sessions/messages) — PG is fast and
  invalidation risk outweighs the gain.
- Cache warming/prefetch, stampede locking / single-flight, LRU tuning,
  Redis eviction policy config, clustering/HA.
- A cache-admin API or metrics dashboard (structlog counters only).
- Per-tenant namespacing (single-user MVP; `owner_id` still reserved).

## Acceptance Criteria

### Foundation (R1)
- [ ] Empty `KB_REDIS_URL` **or** `KB_CACHE_ENABLED=false`: `NullCache`
      everywhere, no redis client constructed, behavior byte-identical to
      today (hard regression test; zero Redis connections in the suite).
- [ ] A simulated Redis fault on `get`/`set` degrades: request still
      succeeds (miss ⇒ recompute), one `cache_error` warning, no key
      contents in logs.
- [ ] Shared client built once per process and closed on shutdown; keys
      all carry the `kb:c1:` version prefix.

### Embeddings (R2)
- [ ] Second identical `embed_texts` for a text returns the cached vector
      without a provider call (assert underlying provider not invoked);
      order preserved; a mixed hit/miss batch calls the provider for the
      misses only.
- [ ] Vectors round-trip byte-exact through the cache; key varies by
      model and dim.

### Agent results (R3)
- [ ] Second `summarize_document` for an unchanged document returns the
      cached summary with **no** agent run (assert agent not called);
      editing content changes `content_hash` ⇒ cache miss ⇒ fresh run.
- [ ] Second `associate_document` within the TTL hits cache; expiry (or a
      bumped model) recomputes. Result payload equals the computed one
      (latency_ms aside).

### Search (R4)
- [ ] Identical query within the TTL and with no intervening write hits
      cache (retriever legs not run — assert ES/vector legs not called);
      a `DocumentService` create/update/delete bumps `epoch` ⇒ next
      identical query misses and recomputes.
- [ ] `limit` / `tag` variants key independently; hydrated content in a
      cached outcome is never a soft-deleted document after an epoch bump.

### Cross-cutting
- [ ] Gates green: `ruff check` / `ruff format --check` / `mypy src` /
      `pytest` (offline, no Redis); optional `live_redis`-marked smoke
      does one real round trip per domain and is deselected by default.
- [ ] `.env.example` documents `KB_CACHE_ENABLED` + every `KB_CACHE_*_TTL_S`.
