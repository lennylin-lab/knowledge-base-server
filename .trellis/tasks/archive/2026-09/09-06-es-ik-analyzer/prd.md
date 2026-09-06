# Add IK analyzer to Elasticsearch for Chinese corpus

## Goal

Chinese documents indexed into Elasticsearch are currently analyzed by the
default `standard` analyzer, which reduces CJK text to single characters.
BM25 over single characters degrades both recall (no word-level matching) and
relevance ranking for the search leg of hybrid retrieval. Ship the
[analysis-ik](https://github.com/infinilabs/analysis-ik) plugin and use it in
the chunk index mapping so Chinese corpora are word-segmented at index and
search time.

User value: 中文语料的全文检索按「词」而非「单字」匹配，显著提升 BM25 召回与相关性排序。

## Confirmed facts (repository / environment evidence)

- ES 8.17.3 single node via `docker-compose.yml:18-32`, no plugins installed;
  index name `kb_documents` (`src/app/core/config.py:24`).
- Explicit mapping lives in `src/app/search/es.py` `_CHUNK_MAPPINGS`;
  `title` and `chunk_text` are plain `text` fields → `standard` analyzer.
- Query layer (`src/app/search/queries.py`) builds `multi_match` without an
  explicit analyzer → it inherits each field's `search_analyzer`; **no query
  code changes needed**.
- IK plugin distributes version-matched zips at
  `https://get.infini.cloud/elasticsearch/analysis-ik/8.17.3`
  (verified HTTP 200 on 2026-09-06).
- Live environment: local ES is up and `kb_documents` already holds 40 docs →
  the migration runbook is a real, exercised path, not theoretical.
- `python -m app.cli reindex` sweeps only `pending`/`failed` by design
  (`src/app/cli.py:118-119`: "Re-indexing done documents (full rebuild) is
  deliberately not offered").
- `IndexingPipeline.process_document(doc_id)` without `expected_updated_at`
  re-indexes any document idempotently (deterministic doc ids), and calls
  `ensure_index` which recreates a missing index with the *current* mapping
  (`src/app/rag/indexer.py:135`).
- PG: `documents.index_status` is enum type `index_status` with lowercase
  values (`src/app/models/document.py:49-56`).
- ES-marked tests run against a live ES and auto-skip when unreachable
  (`tests/conftest.py` probe contract); there is no CI directory.

## Requirements

- **R1 — ES image ships the IK plugin.** The compose `elasticsearch` service
  builds a custom image: official `elasticsearch:8.17.3` + `analysis-ik`
  installed at build time, with the ES version pinned in one place (build ARG
  fed from compose) so image and plugin version cannot drift.
- **R2 — Chunk index mapping uses IK.** `title` and `chunk_text` get
  `analyzer: ik_max_word` (index time, fine-grained) and
  `search_analyzer: ik_smart` (search time, coarse-grained). `keyword` /
  `integer` fields untouched.
- **R3 — Migration runbook for existing installs.** README documents: rebuild
  the ES image, drop the old index, reset `done → pending` in PG, sweep with
  `python -m app.cli reindex`. No new CLI surface: the "full rebuild not
  offered" decision stays intact; ops composes existing primitives.
- **R4 — Tests pin the new contract.** `ensure_index` mapping assertions cover
  `analyzer`/`search_analyzer`; a live `_analyze` check proves a Chinese
  sentence tokenizes into multi-character words (fails if the plugin is
  missing).

## Acceptance criteria

- [ ] **A1** `docker compose build elasticsearch && docker compose up -d` brings
      up ES whose plugin list contains `analysis-ik`.
- [ ] **A2** A fresh index created by `ensure_index` shows
      `ik_max_word`/`ik_smart` on both `title` and `chunk_text`
      (live `get_mapping` assertion in `tests/test_es_store.py`).
- [ ] **A3** Live `_analyze` with `ik_max_word` on a Chinese sentence returns
      multi-character word tokens.
- [ ] **A4** `uv run ruff check .`, `uv run mypy`, `uv run pytest` all pass; the
      ES-marked tests run against the local live ES (not skipped).
- [ ] **A5** README documents the upgrade runbook and it is verified against the
      live local deployment: the existing 40 docs are re-indexed with the IK
      mapping (`docs.count` restored after the sweep).
- [ ] **A6** Query bodies unchanged: no new analyzer keys in `queries.py`;
      existing `tests/test_es_queries.py` stay green untouched.

## Out of scope

- IK remote/custom dictionaries (main dict, stopwords, hot reload).
- Settings-configurable analyzer names (deferred — see `design.md` D3).
- pgvector / embedding / chunker changes; ES security, multi-node, aliases or
  zero-downtime index swapping.
