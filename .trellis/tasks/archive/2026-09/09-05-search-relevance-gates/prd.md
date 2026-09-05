# Search relevance quality gates

## Goal

Stop the hybrid retriever from padding search (and RAG) results with
low-quality hits when the corpus is small or the query is a weak match.
Today `limit` always returns up to N ranked chunks with no absolute
relevance floor — unrelated documents routinely appear merely because
they are the "least bad" nearest neighbors.

User value: search and chat sources surface only chunks that pass explicit
quality gates; empty results are preferred over noise.

## Background

Confirmed from the codebase (2026-09-05):

- Retrieval is **chunk-level** (`document_id` + `chunk_index`); RRF fuses
  BM25 and pgvector legs, then slices `[:limit]` with no threshold
  (`rag/retriever.py`).
- The vector leg always returns K nearest neighbors regardless of distance
  (`repositories/document_chunk.py` `search_similar`).
- The BM25 leg has no `min_score` (`search/queries.py`).
- RRF scores are rank-derived (`1/(k+rank)`), not absolute relevance.
- Search API and QAAgent/WritingAgent share one `Retriever.retrieve()` call
  path.

Prior discussion ruled **out**:

- Client-side-only workarounds (lower limit, UI score cutoffs).
- Document-level deduplication (keep one best chunk per document) — **deferred,
  not in this task**.

## Requirements

1. **Vector leg gate**: drop chunks whose cosine distance exceeds a configured
   maximum before they enter RRF fusion.
2. **BM25 leg gate**: drop chunks whose ES `_score` is below a configured
   minimum before they enter RRF fusion.
3. **Post-fusion relative gate**: after RRF, drop hits whose fused score falls
   below a configured fraction of the top hit's fused score (top-gap cutoff).
4. **Empty over noise**: when no chunk passes all applicable gates, return
   `items: []` — never pad to `limit` with sub-threshold hits.
5. **Settings-driven defaults**: thresholds live in `Settings` (`KB_*` env),
   with conservative defaults suitable for small corpora; tunable without
   code changes.
6. **Shared retriever behavior**: gates apply inside `Retriever.retrieve()` so
   search, QAAgent, and WritingAgent all benefit consistently.
7. **Observability**: extend `SearchHit` (and the internal retrieval model) with
   optional per-leg raw signals (`es_score`, `vector_distance`) so callers and
   tests can verify gating; ranks and fused `score` remain as today.
8. **Degradation preserved**: existing BM25-only / vector-leg-failure
   degradation paths unchanged; gates apply to whichever legs actually ran.
9. **Tests**: unit tests for gate helpers (offline); retriever integration
   tests proving (a) strong queries still hit, (b) weak/unrelated queries
   return fewer or zero items despite `limit`, (c) gates compose correctly
   with tag filter and soft-delete visibility.

## Out of Scope

- Client-side filtering or dynamic limit heuristics in the API consumer.
- Document-level deduplication or "one chunk per document" collapsing.
- Per-caller strictness profiles (`browse` vs `rag` separate thresholds) —
  single global Settings set for MVP; follow-up if tuning diverges.
- Query-time `min_relevance` / `strict` API parameters.
- Re-ranking, cross-encoders, pagination, schema/migration changes.
- Re-calibrating RRF `k` or `CANDIDATE_POOL`.

## Acceptance Criteria

- [ ] Unrelated query against a small seeded corpus (scripted embeddings with
      no neighbor): retriever returns **zero** items even when `limit=10`.
- [ ] Distinctive-term BM25 query against seeded corpus: at least one relevant
      chunk returned with `es_score` above the configured BM25 floor.
- [ ] Vector-neighbor query (scripted embedding match): relevant chunk returned
      with `vector_distance` at or below the configured ceiling.
- [ ] Post-fusion gate removes trailing low-relative-score hits when the top
      hit is strong but lower ranks are weak (unit-tested with synthetic
      scores).
- [ ] `GET /api/v1/search` response includes new optional fields
      (`es_score`, `vector_distance`) on hits where the leg contributed.
- [ ] Chat/QAAgent path uses the same gated retriever (no duplicate logic in
      agents).
- [ ] All applicable quality gates can be disabled via Settings sentinels
      (documented in `design.md`) for test environments.
- [ ] Gates green: `uv run ruff check`, `ruff format --check`, `mypy src`,
      `uv run pytest` (db+es integration where marked).

## Risks and Deferred Items

- **BM25 absolute scores** vary with corpus size; the chosen default
  `SEARCH_BM25_MIN_SCORE` may need adjustment after real-data evaluation —
  document tuning procedure in `design.md`, not a blocker for MVP.
- **Per-caller strictness** (stricter RAG than browse search) deferred until
  usage data shows a need.
- **Document deduplication** deferred per product decision.
