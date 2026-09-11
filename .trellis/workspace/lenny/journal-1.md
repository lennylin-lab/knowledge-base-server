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


## Session 8: Close irrelevant-query noise gates: BM25 identity coverage + vector rescue on-domain trigger (0.62)

**Date**: 2026-09-10
**Task**: Close irrelevant-query noise gates: BM25 identity coverage + vector rescue on-domain trigger (0.62)
**Branch**: `main`

### Summary

Fixed both irrelevant-query noise holes. Hole 2: bm25_chunk_query identity group (title/heading_path) now inherits minimum_should_match 70% from SEARCH_BM25_MIN_COVERAGE, so a lone stopword title hit no longer activates the BM25 leg; single-term queries unaffected. Hole 1: new SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE=0.62 — when the primary vector tier empties, rescue fires only if leg_min <= 0.62, else the leg stays empty. Live calibration on the real 15-doc corpus (new es+live_llm probe test) showed the in-domain (0.473-0.652) and off-domain (0.656-0.774) leg_min bands touch 0.004 apart — no clean threshold; user approved precision-first 0.62 (only bare 事务 at 0.652 loses vector rescue; its BM25 leg still returns results). Verified live: all five noise queries return 0 items (was 14-32); Redis 分布式锁/MySQL 事务隔离 recall unchanged. Also re-anchored the 09-08 D1/D2 live-ES regressions to the current corpus (useState/lru_cache/React 渲染与并发) after the corpus swap stranded the old anchors; full suite 423 passed / 0 failed. Rollback switches: trigger >= 2.0 or empty min_coverage.

### Git Commits

| Hash | Message |
|------|---------|
| `aef7bea` | (see git log) |
| `9db9635` | (see git log) |
| `964dcd0` | (see git log) |
| `8d84e1b` | (see git log) |
| `60bc581` | (see git log) |

### Status

[OK] **Completed**


## Session 9: Vector rescue scoped to lexical-failure backstop (BM25-empty gate)

**Date**: 2026-09-10
**Task**: Vector rescue scoped to lexical-failure backstop (BM25-empty gate)
**Branch**: `main`

### Summary

Closed the q=python topical-neighbor leak: filter_vector_rows_with_rescue gains a bm25_leg_empty precondition (zero gated BM25 survivors) so the vector rescue tier fires only on genuine lexical failure; Retriever.retrieve wires len(kept_es_hits)==0. Unit/integration tests pin suppression (vector_rescued==0 with BM25 hits) and preserved vocabulary-mismatch recall; live E2E verified q=python returns only the 6 genuine Python/FastAPI chunks and the five archived out-of-domain queries still return 0. Spec: new 7-section 'Vector rescue is a lexical-failure backstop' scenario in search-guidelines.md with the distance-interleaving finding and trigger-recalibration caveat (probe with BM25-empty queries).

### Git Commits

| Hash | Message |
|------|---------|
| `f66870a` | (see git log) |
| `5e128e7` | (see git log) |
| `066b3e6` | (see git log) |

### Status

[OK] **Completed**


## Session 10: History-aware query rewriting for chat follow-ups

**Date**: 2026-09-11
**Task**: History-aware query rewriting for chat follow-ups
**Branch**: `main`

### Summary

Implemented 09-10-query-rewrite-followups end to end: new toolless rewrite agent (agents/rewrite.py + prompts/rewrite.md, reuses qa.load_prompt) and a best-effort _rewrite_query step in ChatService.ask that turns anaphoric follow-ups into self-contained retrieval prompts using the last N=3 history turns; original question stays persisted and in history (R3); first turn / stateless / KB_CHAT_QUERY_REWRITE_ENABLED=false are byte-identical; rewrite failure degrades to raw with the stream reaching DoneEvent. Check agent fixed a vacuous-wiring test gap (assert the recorded run prompt, not just StubRetriever.calls) plus a format nit; gates green: ruff check + ruff format --check, strict mypy (63 files), 432 tests (7 new AC tests). Specs updated: search-guidelines.md gains the history-aware query rewrite contract scenario; quality-guidelines.md gains FunctionModel message-history fake gotchas (last-user-prompt recording; prompt-wiring needs its own assertion).

### Git Commits

| Hash | Message |
|------|---------|
| `56d85d5` | (see git log) |
| `0f75035` | (see git log) |
| `f7d4648` | (see git log) |
| `9ba7898` | (see git log) |

### Status

[OK] **Completed**


## Session 11: Token-based history budget + long-document guardrail

**Date**: 2026-09-11
**Task**: Token-based history budget + long-document guardrail
**Branch**: `main`

### Summary

Implemented 09-11-token-history-budget: chat history window now budgeted by tokens (CHAT_HISTORY_TOKEN_BUDGET=2000) with the measure injected into a still-pure select_history_window; long-turn guardrail truncates oversized turns with a visible marker in detached history copies only (persisted rows keep full content; fraction >= 1.0 disables). New llm/tokens.py build_token_counter: tiktoken with o200k_base fallback + deterministic CJK heuristic, never raises; encoder built LAZILY on first history assembly because tiktoken's cold-cache get_encoding does an un-timed network fetch (eager __init__ construction would network in stateless tests). tiktoken promoted to direct dep. Breaking config rename (CHAR_BUDGET -> TOKEN_BUDGET) grep-verified clean. Check agent found only an under-curated check.jsonl (fixed; all five backend specs); gates green: ruff + format, strict mypy, 449 tests — proven network-free by re-running under a socket blocker with empty TIKTOKEN_CACHE_DIR. Specs: new chat-guidelines.md (budget+guardrail contract, ready for the rolling-summary follow-up); quality-guidelines.md gained the tiktoken hidden network-fetch gotcha + no-network proof technique.

### Git Commits

| Hash | Message |
|------|---------|
| `c990389` | (see git log) |
| `eda8061` | (see git log) |
| `d2103b5` | (see git log) |
| `07cdc0b` | (see git log) |

### Status

[OK] **Completed**


## Session 12: Rolling conversation summary beyond the history window

**Date**: 2026-09-11
**Task**: Rolling conversation summary beyond the history window
**Branch**: `main`

### Summary

Implemented 09-11-rolling-history-summary: evicted turns now fold incrementally into chat_sessions.rolling_summary (migration 0007, up/down round-trip verified) with a monotone uuid7 id-ordering watermark (summarized_through_id) that never re-folds on window widening; summary injected as a labeled synthetic request/response pair ahead of in-window history with budget reservation (CHAT_SUMMARY_MAX_TOKENS=400) so it cannot be evicted; maintenance is persist-time best-effort after the streaming try/except before DoneEvent (latency_ms fixed first), read txn closed before the agent LLM call, never raises into ask, empty output treated as failed fold. KB_CHAT_ROLLING_SUMMARY_ENABLED=false restores cliff eviction byte-identically. Check: full-scope 13 files, 0 defects; 468 tests green (18 new DB tests), ruff + strict mypy clean. Specs: rolling-summary contract (watermark ordering, maintenance phasing, reservation formula) into chat-guidelines.md; create_all-does-not-ALTER test-DB gotcha into quality-guidelines.md.

### Git Commits

| Hash | Message |
|------|---------|
| `19bcc6f` | (see git log) |
| `f410869` | (see git log) |
| `33c14c1` | (see git log) |
| `b000064` | (see git log) |

### Status

[OK] **Completed**


## Session 13: Follow-up residual-noise evaluation (direction #4, no-go)

**Date**: 2026-09-11
**Task**: Follow-up residual-noise evaluation (direction #4, no-go)
**Branch**: `main`

### Summary

Executed 09-11-followup-noise-eval against the live 15-doc corpus (37+2 chunks; 2 orphan ES chunks of a deleted predecessor doc recorded as hygiene note, never surfaced). 6 follow-ups over 5 axes (anaphora, comparative, topic shift, ellipsis, out-of-domain), three-way measurement raw/rewritten/ideal at limit=8 with actual rewritten strings captured from the shipped rewriter. Results: mean noise 5.50 -> 3.17 (ideal 2.67), rewrite lift 2.33 = 82% of achievable reduction, recall 3/6 -> 6/6, out-of-domain follow-up returns 0 in ALL conditions; 3 end-to-end chat spot-checks corroborate. Decision: NO-GO per design rule (0.50 residual gap, F6 residual is N=1 with no recall cost — watched pattern, no follow-up task). Check agent verified all 5 ACs incl. live re-run reproduction of the F6 row, fixed 3 findings.md accuracy issues, removed leftover /tmp scratch dir; zero production code changes. Spec: follow-up noise calibration record appended to search-guidelines.md. Direction #4 of the multi-turn issue is now closed.

### Git Commits

| Hash | Message |
|------|---------|
| `998cf21` | (see git log) |

### Status

[OK] **Completed**


## Session 14: Carry prior-run sources into follow-up turns
<!-- trellis-session: v=2 fp=3c0b09d6260c1014 -->

**Date**: 2026-09-12
**Task**: Carry prior-run sources into follow-up turns
**Branch**: `feat/sources-carry-forward`

### Summary

Implemented sources carry-forward: nullable JSONB chat_messages.sources (migration 0008), carried SSE first batch + collector seeding + labeled synthetic pair in services/chat.py, qa.md citation carve-out, CHAT_SOURCES_CARRY_ENABLED flag with byte-identical disabled path. Ruff/mypy clean, 476 tests pass, migration round-trip verified. Spec scenario added to chat-guidelines.

### Git Commits

| Hash | Message |
|------|---------|
| `a197744` | feat(chat): carry prior-run sources into follow-up turns |
| `8497f67` | docs(spec): carried-sources scenario in chat guidelines |
| `19a7e05` | chore(task): plan artifacts for 09-11-sources-carry-forward |

### Status

[OK] **Completed**
