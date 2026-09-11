# Implement: Redis Cache Layer

Ordered checklist. Gates after each group. Each group (Q2–Q4) is an
independently shippable, coherent stopping point — the recommended
rollout order (value ↓ / risk ↑). Commit per group or once at the end.

## Q1 — Foundation (R1) — required before any domain

- [ ] `uv add redis` (already in `uv.lock` 5.3.1 via arq — promotes it to
      a direct dep); verify redis-py asyncio API names
      (`redis.asyncio.from_url`, `get`/`set(ex=)`/`incr`, `aclose`) —
      note deviations.
- [ ] `core/config.py`: `KB_CACHE_ENABLED` + four `KB_CACHE_*_TTL_S`
      fields with the documented defaults; `.env.example` block.
- [ ] `core/cache.py`: `Cache` Protocol, `NullCache`, `RedisCache`
      (every op try/except → `cache_error` warn + degrade, never raises),
      `kb:c1:` version constant.
- [ ] `api/deps.py`: `get_cache()` process-lifetime accessor
      (effective-enable = `CACHE_ENABLED and REDIS_URL`; else shared
      `NullCache`), `close_cache()` added to `main.py::app_lifespan`'s
      existing `finally` next to `close_arq_pool()`.
- [ ] directory-structure.md: one-line note — `redis` import confined to
      `core/cache.py` (infra-import convention).
- [ ] `tests/test_cache.py` (offline): NullCache semantics; RedisCache
      best-effort degradation with a raising fake client; key version
      prefix; `get_cache` returns NullCache when disabled (no client).
- [ ] Validation: ruff + format + mypy; targeted pytest.

## Q2 — Embedding cache (R2) — highest value

- [ ] `llm/embeddings.py`: `CachingEmbeddingProvider` (implements the
      `EmbeddingProvider` Protocol), per-text key + miss-only inner call
      + input-order reassembly; `array('f')` encode/decode helpers with a
      round-trip test.
- [ ] `api/deps.py`: wrap the provider in `embedding_provider_from_settings`
      when effective-enabled (None provider stays unwrapped).
- [ ] `tests/test_embedding_cache.py` (offline, fake inner provider +
      fake cache): full-hit ⇒ no inner call; mixed batch ⇒ inner called
      for misses only, order preserved; vector byte-exact round trip; key
      varies by model/dim; empty batch.
- [ ] Validation: gates; assert the retriever/search/chat wiring still
      passes the unchanged Protocol (no call-site changes needed).

## Q3 — Agent result cache (R3)

- [ ] `services/agents.py`: optional `cache` on `SummarizeService` /
      `AssociationService` (default None = today). Summarize: key
      `summary:{doc_id}:{content_hash}:{model}`, hit ⇒ deserialize +
      recompute latency + `cache_hit`, miss ⇒ run + `set`. Association:
      same shape, assoc key + short TTL, cache the joined result. Null
      `content_hash` ⇒ skip caching.
- [ ] `api/deps.py`: pass cache into `build_summarize_service` /
      `build_association_service`.
- [ ] `tests/test_agent_cache.py` (offline, faked agent + fake cache):
      summarize hit ⇒ agent NOT run; content-hash change ⇒ miss ⇒ run;
      association hit within TTL; payload equality (latency aside); null
      hash ⇒ always miss; error results never cached.
- [ ] Validation: gates.

## Q4 — Search result cache (R4)

- [ ] `rag/retriever.py`: optional `cache`; read epoch at top of
      `retrieve`, build key after `truncate_query`, hit ⇒ skip both legs
      + `cache_hit`, miss ⇒ compute + `set` + `cache_miss`. `SearchOutcome`
      `to_json`/`from_json` (in `rag/`) with a field-exact round-trip test.
- [ ] `services/document.py` + `api/deps.py`: bump `kb:c1:search:epoch`
      via `cache.incr` **after commit** on create/update/delete
      (framework-free seam; best-effort — incr failure never fails the
      write).
- [ ] `_build_retriever` in `api/deps.py`: inject the cache.
- [ ] `tests/test_search_cache.py` (offline, fake retriever legs + fake
      cache): identical query no-write ⇒ legs NOT called; epoch bump ⇒
      miss ⇒ legs called; limit/tag key independence; outcome round-trip;
      soft-delete-then-query never serves the deleted doc (epoch bump path).
- [ ] Validation: full offline suite green, zero Redis connections.

## Q5 — Live smoke (optional, deselected by default)

- [ ] `live_redis` marker (pyproject) — reuse if the arq task already
      added one; else add mirroring `live_mcp` / `live_redis`.
- [ ] `tests/test_cache_live.py` with a redis-reachability probe skip:
      one real round trip per domain (embedding, summary, search epoch
      bump → miss).
- [ ] Manual: compose redis up, `KB_REDIS_URL` set, repeat a search +
      summarize and confirm `cache_hit` in logs; edit a doc, confirm the
      next search misses.

## Review gates

- After Q4 (or the final shipped group): dispatch `trellis-check` — spec
  files in `check.jsonl`, PRD acceptance sweep, best-effort-degradation
  regression, disabled=byte-identical regression, gates re-run.

## Rollback

`KB_CACHE_ENABLED=false` / empty `KB_REDIS_URL` ⇒ NullCache everywhere
(runtime kill switch). Code: single revert, no migrations; flush
`kb:c1:*` to drop cached data.
