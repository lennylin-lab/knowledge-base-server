# Cache Layer Guidelines

> Redis cache contract for `knowledge-base-server`. Established 2026-09-12
> (task `09-12-redis-cache-layer`).

---

## Scenario: Opt-in Redis cache (embeddings, agent results, search)

### 1. Scope / Trigger

- Trigger: infra integration — an optional Redis cache fronts the embedding
  provider, summarize/association agent results, and hybrid search outcomes.
- Governing rule: **the cache is best-effort and must be invisible to
  correctness**. With `KB_CACHE_ENABLED=false` or empty `KB_REDIS_URL`, the
  app is byte-identical to pre-cache behavior (pinned by `tests/test_cache.py`).

### 2. Signatures

- `src/app/core/cache.py` is the **only** module allowed to import `redis`
  (enforced by convention, noted in `directory-structure.md`).

  ```python
  class Cache(Protocol):
      async def get(self, key: str) -> bytes | None: ...
      async def set(self, key: str, value: bytes, ttl_seconds: int | None = None) -> None: ...
      async def incr(self, key: str) -> int: ...  # -1 sentinel on failure
  ```

- Implementations: `NullCache` (no-op get/set, in-process `incr` returning 0)
  and `RedisCache` (redis-py asyncio; `ttl_seconds=0` ⇒ no expiry).
- Wiring: `deps.get_cache()` builds a process-lifetime handle (effective
  enable = `CACHE_ENABLED and REDIS_URL`, otherwise shared `NullCache` — no
  client constructed); `close_cache()` runs in `app_lifespan`'s `finally`.
- Key builder: `cache_key(*parts)` with global version prefix `kb:c1:` —
  bump `c1` (epoch constant) to invalidate every cached shape at once.

### 3. Contracts

- Settings (`KB_CACHE_*`): `CACHE_ENABLED` (bool),
  `CACHE_EMBEDDING_TTL_S` (2592000), `CACHE_SUMMARY_TTL_S` (0 = no expiry),
  `CACHE_ASSOCIATION_TTL_S` (600), `CACHE_SEARCH_TTL_S` (60). All documented
  in `.env.example`.
- Key shapes:
  - embeddings: `kb:c1:emb:{model}:{dim}:{sha256(text)}` per input text
  - summarize: `summary:{doc_id}:{content_hash}:{model}` (self-invalidating
    on content change)
  - association: `assoc:{doc_id}:...` (joined final result, short TTL)
  - search: `kb:c1:search:{epoch}:{sha256(truncated_query)}:{limit}:{tag|-}`
- Serialization: embeddings use `array('d')` float64 encode/decode for
  bit-exact vector round trips; `SearchOutcome` uses explicit field
  `to_json`/`from_json` (never pickle — UUIDs/NamedTuples).
- Search invalidation: `DocumentService._bump_search_epoch()` calls
  `cache.incr(SEARCH_EPOCH_KEY)` **strictly after the DB commit**, best-effort
  (failure never fails the write).

### 4. Validation & Error Matrix

- Every Redis op wrapped in try/except → `cache_error` warning with
  op/domain/error_class **only** (never a key, query, or document text) →
  degrade: `get` returns `None`, `set` drops, `incr` returns `-1` sentinel.
- Unusable cached payload (e.g. corrupt JSON) → treated as miss, recompute.
- `content_hash IS NULL` on a document ⇒ agent result is **never cached**.
- Error/failed agent results are never cached.

### 5. Good/Base/Bad Cases

- Good: hit on summarize key after content unchanged; `latency_ms` recomputed
  on hit; `cache_hit` replaces the `agent_run_started/finished` event pair.
- Base: cache disabled → `NullCache` everywhere, no redis client constructed,
  zero Redis connections in the default test run.
- Bad: caching before the 404 check, logging key contents, caching an error
  result, pickling `SearchOutcome`.

### 6. Tests Required

- `tests/test_cache.py` — primitive behavior, disabled-path byte-parity,
  key builder prefix, degradation logging (assert no key contents in output).
- `tests/test_embedding_cache.py` — miss-only inner calls, order reassembly,
  float64 byte-exact round trip, model/dim key isolation, `None` provider
  stays unwrapped.
- `tests/test_agent_cache.py` — 404-before-cache-lookup, null
  `content_hash` never cached, errors never cached, hit recompute of
  `latency_ms`.
- `tests/test_search_cache.py` — key built after `truncate_query`, epoch bump
  after commit on create/update/delete, `SearchOutcome` round trip incl. all
  counters, unusable payload → recompute, `cache=None` retains old behavior.
- `tests/test_cache_live.py` — `live_redis` marker (deselected by default),
  real Redis round trip.
- Shared double: `FakeCache` in `tests/fakes.py` honors the degradation
  contract (fault modes log `cache_error` and degrade like `RedisCache`).

### 7. Wrong vs Correct

#### Wrong

```python
# Cache lookup before document load: a stale/absent doc gets served, 404 contract breaks
cached = await cache.get(f"summary:{doc_id}")
if cached is None:
    doc = await repo.get(doc_id)  # 404 raised too late
```

#### Correct

```python
doc = await repo.get(doc_id)  # 404 contract first
if doc.content_hash is not None:
    cached = await cache.get(f"summary:{doc_id}:{doc.content_hash}:{model}")
```

---

## Gotchas

> **Warning**: `array('f')` (float32) is NOT acceptable for embedding
> serialization — use `array('d')` (float64) so vectors round-trip bit-exact;
> cosine distances computed from re-floated vectors diverge in tests.

> **Warning**: the summary key is self-invalidating via `content_hash`; do
> not add a TTL-based "summary invalidation" scheme on top — change the
> content, the key changes.

**Language**: All documentation is written in **English**.
