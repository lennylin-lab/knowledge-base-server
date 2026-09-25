# Implementation Plan

Validation baseline (run after each step, full set before commit):

```bash
uv run ruff format --check . && uv run ruff check .
uv run mypy .
uv run pytest -m "not es and not live and not live_llm and not db" -q   # fast offline
uv run pytest -m db -q        # real-PG suite (needs compose PG)
uv run pytest -m es -q        # live-ES suite (needs compose ES; includes calibration probes)
```

Live-LLM probes (`-m live_llm`) are not required by this task.

## Step 1 — Parse layer: length cap (R1, AC1)

- [ ] `services/document.py`: add `DESCRIPTION_MAX_CHARS = 500` +
      `ValidationError` in `_parse_front_matter` (design §1).
- [ ] `tests/test_documents_service.py`: >500 rejects (422 at API level in
      `tests/test_documents_api.py` too), ==500 passes, absent/empty/whitespace
      unchanged, non-string still rejected.
- Rollback point: parse-only commit; no stored data touched.

## Step 2 — ES mapping + BM25 identity group (R2, AC2/AC3)

- [ ] `search/es.py`: `description` in `_CHUNK_MAPPINGS` (IK pair);
      `replace_document_chunks(..., description: str, ...)` writes it into
      every chunk `_source`.
- [ ] `search/queries.py`: `_DESCRIPTION_BOOST = 1.5` (placeholder pending
      Step 8 calibration) + extend `_IDENTITY_FIELDS`; docstring updated
      (identity group = title + heading_path + description).
- [ ] `tests/test_es_store.py`: mapping assertion includes `description`.
- [ ] `tests/test_indexer.py`: ES doc body carries description (fake store
      asserts `_source["description"]`); protocol signature updated.
- [ ] `tests/test_es_queries.py`: body-pin fields list gains
      `description^1.5`; no-analyzer-key walk still green.
- [ ] `tests/test_es_relevance.py`: probes from design §7 (disposable
      synthetic corpus): description-only recall, no over-boost, empty-parity.

## Step 3 — Chunk embedding input (R3, AC4)

- [ ] `rag/indexer.py::embedding_input(title, chunk, *, description="")` —
      golden-string shape per design §3; `process_document_raising` passes
      `document.description`.
- [ ] `tests/test_indexer.py` + `tests/corpus.py`: golden strings for
      with/without description (corpus derives through the production
      function; add explicit literal-string pins).

## Step 4 — Migration + document embedding (R3, AC4)

- [ ] `models/document.py`: `description_embedding` nullable
      `Vector(EMBEDDING_DIM)` + HNSW cosine index.
- [ ] `alembic/versions/0013_documents_description_embedding.py` (style of
      0012): add column + HNSW index; downgrade reverses.
- [ ] `rag/indexer.py`: single embed batch = `[description]? +
      chunk inputs`; store vector on the document row in the staging
      transaction; empty description → NULL (replace semantics).
- [ ] `tests/test_indexer.py`: fake provider asserts the batch contents
      (description first when non-empty, absent when empty) and the document
      row write/clear.
- Rollback point: schema migration is additive + NULLable; reindex sweep
  repopulates whenever needed.

## Step 5 — Read models & API plumbing (R4, AC5)

- [ ] `repositories/document_chunk.py`: `ChunkRow.document_description` +
      `_LIVE_CHUNK_SELECT` + `_as_chunk_row`.
- [ ] `rag/retriever.py`: `RetrievedChunk.document_description` (default "")
      + `to_json` / `from_json` + hydration mapping.
- [ ] `schemas/search.py::SearchHit`, `services/search.py`,
      `agents/qa.py::to_search_hit`, `schemas/session.py::SourceRef`.
- [ ] Tests: `test_retriever.py` (plumbing), `SearchOutcome` cache round-trip,
      `test_search_api.py` / `test_chat_api.py` / `test_writing_api.py`
      sources expose the field; old-payload (missing key) still validates.

## Step 6 — Agent surfaces (R5, AC6)

- [ ] `agents/qa.py::format_context_blocks`: optional `Summary:` line;
      `qa.md` + `writing.md` grounding clauses.
- [ ] `agents/summarize.py`: `SummarizeDeps.description`,
      `render_document_prompt` / `render_reduce_prompt` Description line;
      `summarize.md` context clause; `SummarizeService` passes it.
- [ ] `agents/association.py`: `AssociationDeps.description`,
      `AssociationCandidate.description`, prompt lines; `association.md`
      signal-guidance clauses.
- [ ] Tests: `test_qa_agent.py` (both branches), `test_summarize_service.py`
      (prompt contains line iff non-empty), prompt-content assertions via
      recorded prompts.

## Step 7 — Association third leg (R6, AC7)

- [ ] `models/document.py` read projection: `TagOverlapRow.description` +
      `NeighborDocumentRow.description` (fold into both existing queries).
- [ ] `repositories/document.py`: `DescriptionNeighborRow` +
      `find_by_description_similarity` (HNSW cosine, tenant filter, exclude
      source, deterministic `(distance, id)` order).
- [ ] `services/agents.py`: `_gather` calls the leg iff the source embedding
      is non-null; `_merge_candidates(vector, description, tag)` with
      `_description_signal`; `AssociationCandidate.description` plumbed.
- [ ] `tests/test_association_service.py`: three-leg merge determinism,
      signal string, self-exclusion, soft-deleted invisible, empty source
      description → two-leg behavior byte-identical, candidate description in
      prompts (scripted `FunctionModel` prompt capture).

## Step 8 — Calibration + pins (R8, AC8)

- [ ] Run Step 2's live probes; if the 1.5 hypothesis fails a probe, adjust
      `_DESCRIPTION_BOOST` (candidate ladder 1.5 → 1.25 → 1.0) and re-run.
- [ ] Fill design.md § Calibration Record (final fields, boost, matrix,
      reason); align `tests/test_es_queries.py` pin with the final value.
- Review gate: calibration record + probe output pasted into the PR
  description.

## Step 9 — Runbook + spec (R7, AC9)

- [ ] README runbook: migrations list gains the entry (drop index + reset
      `pending` + `python -m app.cli reindex` re-embeds chunks and
      descriptions).
- [ ] `.trellis/spec/backend/search-guidelines.md`: description contract
      (mapping field, identity group, embedding inputs incl. document-level
      description vector) — Phase 3.3 convention.

## Final gate (Phase 2.2 last iteration)

- [ ] Full suite: `uv run ruff format --check . && uv run ruff check . &&
      uv run mypy . && uv run pytest -q` (offline + db + es marks per
      quality-guidelines).
- [ ] Cross-layer spot check: description flows content → PG → ES `_source` →
      BM25 hit → `ChunkRow` → `RetrievedChunk` → `SearchHit` → context block
      and `SourceRef` without a single field rename drift.
- [ ] Issue #4 DoD checklist diffed against AC1–AC9.
