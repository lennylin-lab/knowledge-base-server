# Hybrid Retrieval: BM25 + Vector + RRF, Public Search Endpoint

## Goal

Make the indexed data (previous task) retrievable: a hybrid search engine
combining Elasticsearch BM25 and pgvector cosine similarity, fused with
Reciprocal Rank Fusion (RRF), exposed through a public
`GET /api/v1/search` endpoint. This is both a user-facing feature
(full-text search of the knowledge base) and the data foundation the
future QAAgent retrieves with.

No schema changes, no agents, no MCP in this task.

## Requirements

1. **BM25 query builders** (`search/queries.py`): pure functions building
   the ES DSL — multi-match on `chunk_text` with a `title` boost, optional
   `tags` keyword filter, bounded `size`. No I/O; dicts in, dicts out.
2. **Vector leg** (`repositories/document_chunk.py` `search_similar`):
   cosine-distance ordering over `document_chunks.embedding`, **joined to
   live documents** (`deleted_at IS NULL`) — soft-deleted documents' chunks
   must never surface, and chunk rows hydrate with document title/tags in
   the same query.
3. **Fusion** (`rag/retriever.py`): run both legs concurrently (ES + PG),
   fuse with RRF (`score = Σ 1/(k + rank)`, k=60, candidate pool 50/leg),
   return top `limit` hits with document metadata + chunk text + fused
   score + per-leg ranks. Pure fusion function, unit-testable.
4. **Hydration from PG**: ES returns ranked `(document_id, chunk_index)`
   keys only; PG hydrates chunk text + document title/tags with the
   live-document filter. PG is the single source of truth for visibility —
   ES-side staleness (soft-deleted docs still indexed) cannot leak results.
5. **Public endpoint** `GET /api/v1/search?q=&limit=&tag=`:
   - `q` required, min length 1 (empty → 422 envelope)
   - `limit` 1..50, default 10
   - `tag` optional, normalized like the documents list
   - Response: `mode` (`"hybrid"` | `"bm25"`) + `items` (document_id,
     document_title, document_tags, chunk_index, content, score,
     es_rank, vector_rank — ranks `null` when the leg didn't return it)
   - Router thin (`api/v1/endpoints/search.py`), orchestration in
     `services/search.py`, schemas in `schemas/search.py`
6. **Degradation** (user decision): when `OPENAI_API_KEY` is unset/empty
   the retriever runs BM25-only with `mode="bm25"` and a warning log at
   startup-ish wiring time; a mid-search embedding failure (key present
   but provider errors) also degrades to BM25 with a warning — search
   stays available, never 5xx over a missing vector leg.
7. **Tests**: RRF fusion units (offline, hand-computed), ES query-builder
   units (offline, dict shapes), retriever integration (db+es, seeded via
   the existing IndexingPipeline with a scripted embedding fake so the
   vector leg has known neighbors), endpoint contract (status codes,
   envelope, mode field), soft-delete invisibility, tag filtering on both
   legs, degradation paths.

## Out of Scope

- Search pagination/cursors (limit-bounded MVP; full pagination later)
- Snippet highlighting/matching offsets (chunk text is already ≤1600 chars)
- QAAgent / chat / SSE (next task consumes this retriever)
- Re-ranking with a cross-encoder, semantic caching
- ES index aliases / zero-downtime reindexing
- Multi-tenant/ownership filters

## Acceptance Criteria

- [ ] Seeded corpus, distinctive term search: correct document top-ranked
      via the BM25 leg (tested with db+es up)
- [ ] Query whose embedding (scripted fake) equals a chunk's embedding:
      that chunk top-ranks via the vector leg; both legs present in one
      response with `mode: "hybrid"`
- [ ] RRF fusion unit test: hand-computed ranks produce expected order and
      scores (pure function, offline)
- [ ] Soft-deleted document's chunks appear in NO result (both legs)
- [ ] `tag=` filter narrows results on both legs
- [ ] Empty `q` → 422 envelope; `limit=0`/`limit=51` → 422; missing
      `q` → 422
- [ ] No API key: endpoint still 200 with `mode: "bm25"`, warning logged;
      provider failure mid-search degrades the same way
- [ ] Offline (compose down): `uv run pytest` green with visible skip
      reasons; RRF + query-builder units run offline
- [ ] Gates green: ruff check / ruff format --check / mypy src / pytest
