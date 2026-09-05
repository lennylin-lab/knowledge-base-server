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
