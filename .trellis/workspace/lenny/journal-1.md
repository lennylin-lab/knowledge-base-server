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
