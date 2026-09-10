# Journal - lenny (Part 1)

> AI development session journal
> Started: 2026-08-27

---



## Session 1: Content hash guard skips duplicate reindexing

**Date**: 2026-09-05
**Task**: Content hash guard skips duplicate reindexing
**Branch**: `main`

### Summary

Added documents.content_hash (sha256 hex, NULL=changed) with migration 0006 incl. pgcrypto backfill; update_document now resets index_status and enqueues indexing only when content hash/resolved title changed or status is not DONE, so byte-identical saves of indexed documents no longer re-embed. 9 new service tests; gates green (2 test_indexer failures verified environmental: KB_REDIS_URL + Redis down, reproduced at HEAD worktree). Spec: guard contract in database-guidelines, Redis-env test trap in quality-guidelines.

### Git Commits

| Hash | Message |
|------|---------|
| `c2b031c` | (see git log) |
| `b66aae1` | (see git log) |

### Status

[OK] **Completed**


## Session 2: Search relevance quality gates

**Date**: 2026-09-05
**Task**: Search relevance quality gates
**Branch**: `main`

### Summary

Implemented BM25/vector/relative RRF relevance gates in rag/retriever.py (empty-over-noise), Settings thresholds KB_SEARCH_* with disable sentinels wired via deps into search+chat+writing, SearchHit es_score/vector_distance fields, search_executed gate counters; 14 offline gate unit tests + retriever/API gate integration tests; check verdict PASS, all 8 PRD ACs evidenced; spec contract captured in directory-structure.md and logging-guidelines.md.

### Git Commits

| Hash | Message |
|------|---------|
| `09e2c63` | (see git log) |
| `fb77ee4` | (see git log) |

### Status

[OK] **Completed**


## Session 3: Short-query vector gate calibration

**Date**: 2026-09-06
**Task**: Short-query vector gate calibration
**Branch**: `main`

### Summary

Two-tier head-rescue vector gate (rescue only when the 0.45 ceiling empties the leg; window min(leg_min+0.15, 0.85)) restores short-keyword semantic recall while rare-term legs stay ES-dominated; SEARCH_MAX_QUERY_LENGTH=256 single-point truncation removes the ES clause-limit 502 for long CJK queries; live_llm probe calibrated defaults against Qwen3-Embedding-4B (CJK heads 0.48-0.61 vs long-query 0.22); vector_rescued counter in search_executed; 383 tests green, check PASS.

### Git Commits

| Hash | Message |
|------|---------|
| `bba8dad` | (see git log) |
| `77c8693` | (see git log) |

### Status

[OK] **Completed**


## Session 4: ES IK analyzer for Chinese corpus

**Date**: 2026-09-06
**Task**: ES IK analyzer for Chinese corpus
**Branch**: `main`

### Summary

Shipped analysis-ik into a locally built ES 8.17.3 image (hermetic zip install after get.infini.cloud throttled to ~1.6KB/s), set ik_max_word/ik_smart on title+chunk_text in _CHUNK_MAPPINGS (queries.py untouched, inherits search_analyzer), added live mapping+_analyze plugin-contract tests, README upgrade runbook (drop index, reset done->pending, cli reindex), migrated the live 16-doc/40-chunk corpus with zero failures; spec captured in search-guidelines.md.

### Git Commits

| Hash | Message |
|------|---------|
| `5a09cf4` | (see git log) |
| `efd3a70` | (see git log) |

### Status

[OK] **Completed**


## Session 5: Code-aware markdown chunking + code-friendly ES index

**Date**: 2026-09-07
**Task**: Code-aware markdown chunking + code-friendly ES index
**Branch**: `main`

### Summary

Implemented 09-06-code-aware-chunking end to end: fence state machine in rag/chunker.py (no more splitting on # comments inside fences, byte-preserving fence content, oversized fences re-fenced per piece, Chunk dataclass + heading breadcrumbs via chunk_markdown_structured), ES code analyzer subfield (whitespace + word_delimiter_graph, no stopwords) with heading_path field and BM25 boosts while IK stays primary for Chinese, indexer embeds title+breadcrumb+text (PG still stores plain text). Regression tests written first and observed failing; check agent fixed breadcrumb store assertion and heading closing-sequence stripping. 400 unit + 44 live-ES tests green; dev-stack migration executed (drop index, reset pending, reindex 16 docs/40 chunks); vector probe re-measured within defaults. Specs updated: search-guidelines (code analyzer, stopword incident, breadcrumb contract), directory-structure (fence-aware chunking invariant, re-measured vector distances).

### Git Commits

| Hash | Message |
|------|---------|
| `47be781` | (see git log) |
| `4e933c4` | (see git log) |
| `365f8f5` | (see git log) |

### Status

[OK] **Completed**


## Session 6: ES BM25 scoring overhaul: cross-field evidence and coverage gate

**Date**: 2026-09-08
**Task**: ES BM25 scoring overhaul: cross-field evidence and coverage gate
**Branch**: `main`

### Summary

Implemented 09-08-es-bm25-scoring end to end. G1: split the code analyzer — flat code_search (no preserve_original/catenate/flatten_graph) bound via search_analyzer, remove_duplicates on the index side (camelCase tf inflation); D2 phrase-query trap eliminated (ConnectionPool 0->2 hits, async_bulk 0->12). G2: replaced multi_match best_fields with three-group bool.should (identity max-group for title/heading_path C1 overlap, prose, code groups sum). G3: SEARCH_BM25_MIN_COVERAGE=70% minimum_should_match on chunk_text leaf replaces the never-firing absolute floor; SEARCH_BM25_MIN_SCORE retired to 0.0 with rationale. Calibration matrix recorded in design.md (noise queries -> 0, all relevance probes >= 1). Live migration run: 16/16 docs done, 40 chunk docs. Check agent verified all 12 acceptance criteria against live-ES measurements; fixed retriever docstring + design.md files table. 414 unit + 51 live-ES tests green, ruff/mypy clean. Specs updated: search-guidelines (analyzer split rule, D2 warning, scoring scenario), directory-structure (gates block).

### Git Commits

| Hash | Message |
|------|---------|
| `bf03a23` | (see git log) |
| `0d599ad` | (see git log) |

### Status

[OK] **Completed**


## Session 7: Complete Trellis onboarding (00-join-lenny)

**Date**: 2026-09-10
**Task**: Complete Trellis onboarding (00-join-lenny)
**Branch**: `main`

### Summary

Archived the join-lenny onboarding task after completing Trellis orientation. No application code changes in this session.

### Git Commits

(No commits - planning session)

### Status

[OK] **Completed**
