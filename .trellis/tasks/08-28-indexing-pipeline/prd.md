# Indexing Pipeline: Chunking, Embedding, Dual-Store

## Goal

Close the loop the documents slice opened: every write marks
`index_status = pending`; this task makes that status reach `done` by
chunking the markdown, embedding the chunks, and storing them in **both**
pgvector (`document_chunks`) and Elasticsearch (BM25 corpus). After this
task, the retrieval layer (later) has data to read.

`index_status = done` means **both** stores are populated and consistent;
`failed` means the last attempt errored and the document is retriable.

## Requirements

1. **Chunking** (`rag/chunker.py`): markdown-aware, pure, deterministic.
   Splits on heading boundaries (heading text kept with its section),
   merges small sections toward a target size, hard-splits oversized
   sections at paragraph boundaries. Front matter is excluded from chunks
   (title/tags are indexed as fields, not chunk text). No I/O, no globals.
2. **Embedding provider** (`llm/embeddings.py`): `EmbeddingProvider`
   protocol + OpenAI-compatible implementation via the openai SDK with
   `base_url`/`api_key`/model from Settings. Retries/timeouts live here,
   nowhere else. No domain knowledge in this layer.
3. **Chunk storage** (`models/document_chunk.py` + migration `0003` +
   `repositories/document_chunk.py`): one row per chunk, `(document_id,
   chunk_index)` unique, `Vector(1536)` embedding, HNSW cosine index,
   FK `ondelete=CASCADE`. Re-indexing deletes and re-inserts a document's
   chunks in one transaction (derived data, never updated in place).
4. **ES store** (`search/es.py`): client from Settings, index-lifecycle
   helper (create with explicit mapping if missing), bulk-index chunks as
   `"{document_id}:{chunk_index}"` docs with `document_id`, `title`,
   `tags`, `chunk_text` fields; delete a document's previous ES docs
   before re-indexing (idempotent replace).
5. **Pipeline orchestrator** (`rag/indexer.py`): `process_document(doc_id)`
   loads the doc, chunks, embeds, replaces PG chunks, replaces ES docs,
   then flips `index_status` to `done`; any stage error flips it to
   `failed` (small follow-up tx) and is swallowed with a warning log —
   it runs as a background task and must never raise into a response.
   Idempotent: safe to re-run.
6. **Write-path trigger**: document create/update enqueues indexing via
   FastAPI `BackgroundTasks` after the response. Services stay
   framework-free: they receive an injected enqueuer callable; the
   FastAPI adapter lives in `api/deps.py`.
7. **CLI compensation** (`src/app/cli.py`, `python -m app.cli reindex`):
   batch-processes documents with `index_status in (pending, failed)`,
   `--limit` bounded, logs a summary. Covers retry-after-failure and
   backfill.
8. **Tests**: chunker units (offline), indexer with a fake embedding
   provider (db), ES roundtrip behind an ES reachability probe (skip
   offline with visible reason), enqueue wiring (fake enqueuer), CLI
   function-level. Live provider test allowed only under `live_llm`
   marker.

## Out of Scope

- Retrieval/search endpoints (ES BM25 + pgvector + RRF fusion — next task)
- ARQ/Redis worker migration (BackgroundTasks is the MVP trigger)
- Chunk-level incremental indexing (any update re-indexes the whole doc)
- Agents/QA, MCP — untouched
- ES index aliases / zero-downtime reindex operations

## Acceptance Criteria

- [ ] With compose PG+ES up and a fake provider: create or update a
      document → background indexing runs → `GET /documents/{id}` shows
      `index_status: "done"`; `document_chunks` rows exist with embeddings;
      ES docs for the document exist and match the chunk count
- [ ] Update a document → old chunks fully replaced in both stores (no
      stale rows/docs, counts match new content)
- [ ] Embedding/ES failure → `index_status: "failed"`, error logged as
      warning (no content in logs), response unaffected
- [ ] `uv run python -m app.cli reindex` flips a `failed`/`pending`
      document to `done` and prints a summary
- [ ] Offline (compose down): `uv run pytest` green, db/es tests skip
      with visible reasons
- [ ] Migration `0003` upgrade → downgrade → upgrade round-trips
- [ ] Gates green: ruff check / ruff format --check / mypy src / pytest
