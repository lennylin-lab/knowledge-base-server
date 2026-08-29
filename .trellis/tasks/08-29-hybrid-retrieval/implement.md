# Implement: Hybrid Retrieval

Ordered checklist. Gates after each group; single commit at the end
(no schema change).

## R1 — Fusion + query builders (offline-testable first)

- [ ] `rag/retriever.py`: `ChunkKey`, `fuse_rrf` pure function (k=60,
      CANDIDATE_POOL=50), determinism rules
- [ ] `search/queries.py`: `bm25_chunk_query(q, *, size, tag)` dict builder
- [ ] `tests/test_rrf_fusion.py` (hand-computed scores), `tests/test_es_queries.py`
- [ ] Validation: `uv run pytest tests/test_rrf_fusion.py tests/test_es_queries.py`;
      ruff + format + mypy

## R2 — Repository legs + retriever wiring

- [ ] `repositories/document_chunk.py`: `search_similar` (cosine + live-doc
      join + optional tag), `get_live_chunks` (tuple-IN hydration)
- [ ] `rag/retriever.py`: `Retriever.retrieve` — gather legs, provider-None
      ⇒ bm25-only, provider-failure ⇒ degrade+warn, ES-failure ⇒ raise
      SearchIndexError, hydrate, top-limit
- [ ] `tests/fakes.py`: `ScriptedEmbeddingProvider` (text→vector map)
- [ ] `tests/test_retriever.py`: seed via IndexingPipeline; BM25-top,
      vector-top, both-ranks, soft-delete invisible, tag both legs,
      ES-failure raises, provider-failure degrades
- [ ] Validation: db+es tests with compose up; gates

## R3 — Service, endpoint, degradation wiring

- [ ] `schemas/search.py` (SearchHit, SearchResponse with mode)
- [ ] `services/search.py` (SearchService, `search_executed` event)
- [ ] `api/v1/endpoints/search.py` + register in router; `api/deps.py`:
      retriever construction, None-provider when key empty,
      `vector_search_disabled` warn once, ES client not per-request
- [ ] `tests/test_search_api.py`: 200 shape/mode, 422s, no-key ⇒ bm25
- [ ] Validation: full `uv run pytest` (compose up, none skipped except
      live_llm); offline probe-run spot check stays green

## Review gates

- trellis-check dispatch after R3: five spec files, PRD acceptance sweep,
  layering (router→service→rag; search/ no domain logic), gates re-run.

## Rollback

- Single revert; no migration; ES index/chunks untouched.
