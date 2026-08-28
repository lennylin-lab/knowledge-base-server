# Implement: Indexing Pipeline

Ordered checklist. Gates after each group; commit points G1/G2.

## G1 — Chunk model + migration

- [ ] `models/document_chunk.py` per design (Vector(1536) literal, HNSW
      index, unique (document_id, chunk_index), FK CASCADE)
- [ ] `models/__init__.py` re-export
- [ ] `alembic revision --autogenerate -m "document chunks table"` → edit:
      verify HNSW + unique constraint render, `Vector` import present,
      downgrade drops the table
- [ ] `repositories/document_chunk.py`: `replace_for_document`
      (delete + bulk insert, no commit)
- [ ] Validation: `KB_DATABASE_URL=postgresql+asyncpg://kb:kb@localhost:5432/kb uv run alembic upgrade head`
      → `downgrade -1` → `upgrade head`; `uv run mypy src`; `uv run ruff check .`

**Commit G1** — "feat: document_chunks model, migration, repository"

## G2 — Chunker, embeddings, ES store

- [ ] `rag/chunker.py` `chunk_markdown()` + `rag/__init__.py`
- [ ] `llm/embeddings.py`: `EmbeddingProvider` protocol +
      `OpenAIEmbeddingProvider` (LLMProviderError/LLMRateLimitedError)
- [ ] `core/exceptions.py`: add `SearchIndexError`
- [ ] `search/es.py` + `search/__init__.py`: `get_es_client`,
      `ensure_index` (explicit mapping), `replace_document_chunks`
      (delete_by_query + bulk, deterministic ids)
- [ ] `core/config.py`: add `ES_INDEX: str = "kb_documents"`
- [ ] `tests/test_chunker.py` (offline), `tests/test_embeddings.py`
      (fake + protocol; live under `live_llm`)
- [ ] Validation: ruff + format + mypy; `uv run pytest tests/test_chunker.py tests/test_embeddings.py`

## G3 — Pipeline, trigger, CLI

- [ ] `rag/indexer.py`: `IndexingPipeline.process_document` +
      `run_indexing` background entry; repo additions
      (`DocumentRepository.set_index_status`, `list_by_index_status`)
- [ ] `services/document.py`: `ReindexEnqueuer` callable param (default
      None = no-op), enqueue after commit on create/update
- [ ] `api/deps.py`: BackgroundTasks adapter wiring the enqueuer
- [ ] `src/app/cli.py`: `reindex` subcommand (argparse, asyncio.run,
      counts summary)
- [ ] conftest: `es` marker + `_es_reachable()` probe, `FakeEmbeddingProvider`
      fixture, unique ES test index per run
- [ ] `tests/test_indexer.py`, `tests/test_es_store.py`,
      extend `tests/test_documents_service.py` (enqueue capture),
      `tests/test_cli.py`
- [ ] Validation:
  - compose up (PG+ES): `uv run pytest` — all run, none skipped
  - `docker compose stop` both: green with visible skips → start again
  - live smoke (needs real key): create doc via API → status done,
    chunks in PG, docs in ES — optional, fake-provider tests are primary
  - full gates: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest`

**Commit G2+G3** — "feat: indexing pipeline (chunking, embeddings, pgvector+ES) with background trigger and reindex CLI"

## Review gates

- trellis-check dispatch after G3: all five backend spec files, PRD
  acceptance sweep, migration round trip, offline contract, gates re-run.

## Rollback

- After G1: `alembic downgrade -1` + `git revert <g1>`
- After G3: `git revert <g2g3>`; ES index disposable; no data in
  `documents` changed shape (only column values already existing)
