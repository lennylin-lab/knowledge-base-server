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


## Session 15: Redis cache layer for embeddings, agents, and search
<!-- trellis-session: v=2 fp=bc88d38e3bb44e72 -->

**Date**: 2026-09-12
**Task**: Redis cache layer for embeddings, agents, and search

### Summary

Implemented the opt-in Redis cache task 09-12-redis-cache-layer: Cache Protocol/NullCache/RedisCache primitive in core/cache.py (only redis-importing module), CachingEmbeddingProvider with byte-exact float64 vectors, summarize/association result caching preserving the 404-before-lookup contract, search outcome caching with post-commit epoch invalidation, best-effort degradation with keys never logged. Gates passed (ruff/mypy/511 tests, zero Redis offline). Added .trellis/spec/backend/cache-guidelines.md spec scenario.

### Git Commits

| Hash | Message |
|------|---------|
| `1d3dbff` | feat(cache): opt-in Redis cache for embeddings, agent results, and search |

### Status

[OK] **Completed**


## Session 16: Chat SSE progress events
<!-- trellis-session: v=2 fp=1a46605382b9b869 -->

**Date**: 2026-09-13
**Task**: Chat SSE progress events
**Branch**: `main`

### Summary

Added four additive SSE progress events (status, tool_call_started, tool_call_finished, query_rewritten) to chat and writing streaming via a new services/stream_bridge.py RunEventBridge over pydantic-ai event_stream_handler; rewrote _rewrite_query to _resolve_rewrite with RewriteOutcome; fixed _is_tool_failure reading event.part.content; preserved carried-sources-first and citation-order invariants; captured SSE vocabulary/ordering contracts into backend specs. All gates green: 522 tests, strict mypy, ruff. Also recovered the session's commits that were stranded on a detached HEAD by fast-forwarding main to 7cf770c.

### Git Commits

| Hash | Message |
|------|---------|
| `33ba8c9` | feat(chat): additive SSE progress events for status, tool calls, and query rewrite |
| `657c8d0` | test(chat): progress-event order, wire shapes, and MCP soft-failure coverage |
| `4483760` | docs(spec): SSE progress-event vocabulary and ordering contracts |

### Status

[OK] **Completed**


## Session 17: Agent document drafting and persistence
<!-- trellis-session: v=2 fp=fbf1b8d118658571 -->

**Date**: 2026-09-14
**Task**: Agent document drafting and persistence
**Branch**: `feat/agent-document-persistence`

### Summary

Implemented agent-operation domain: AgentOperation/DocumentRevision models with Alembic 0009, AgentOperationService with idempotent create, inspect/resume, and atomic optimistic-concurrency apply (version check before writes, single commit, post-commit indexing enqueue), /operations API with explicit draft/apply/resume. Drafts and interrupted runs stay out of chat history. trellis-check passed (ruff/mypy clean, 534 tests); captured atomic-apply pattern into database-guidelines spec.

### Git Commits

| Hash | Message |
|------|---------|
| `ddbc8b2` | feat(operations): agent document drafting with atomic optimistic-concurrency apply |
| `8e0e32b` | docs(spec): atomic optimistic-concurrency apply pattern |

### Status

[OK] **Completed**


## Session 18: Gateway integration verified end-to-end
<!-- trellis-session: v=2 fp=12049ab31bbc293d -->

**Date**: 2026-09-15
**Task**: Gateway integration verified end-to-end
**Branch**: `main`

### Summary

Ran 09-15-gateway-integration to completion via trellis-implement/trellis-check sub-agents: direct gateway probes (healthz/readyz, gateway-echo non-stream + stream, key mint/revoke) and server e2e /api/v1/chat SSE smoke on a temp port-8010 instance with env-only KB_CHAT_* overrides both passed. No server or gateway defects; tools/MCP non-forwarding recorded as documented capability, no public issue filed. No server code changed, so quality gates not applicable. Spec: added gateway chat-adapter boundary convention to backend/chat-guidelines.md. Follow-ups flagged: gateway tools/MCP forwarding (blocks RAG-through-gateway), server request-id propagation into OpenAI client.

### Git Commits

| Hash | Message |
|------|---------|
| `ea58301` | chore(task): gateway integration verified; record gateway boundary spec |

### Status

[OK] **Completed**


## Session 19: Gateway identity, tenancy, and RBAC implemented
<!-- trellis-session: v=2 fp=bf773575071f09ce -->

**Date**: 2026-09-15
**Task**: Gateway identity, tenancy, and RBAC implemented
**Branch**: `main`

### Summary

Completed 09-15-gateway-integration-v2 across three trellis-implement dispatches plus a full-scope trellis-check pass. Chat provider secrets moved behind the gateway (service-account KB_CHAT_* keys, opt-in live_gateway probe suite, default suite offline). New src/app/auth/ boundary: Principal, JWKS-caching TokenVerifier (iss/aud/sig/exp/leeway/typ, generic 401), constant-time service-account path. Tenant schema via Alembic 0010 (tenants/users/tenant_memberships, deterministic default-tenant backfill). Resource isolation via 0011 (non-null tenant_id on documents/revisions/sessions/operations; scoping through services, repos, ES term filter, cache keys, index-queue payloads; uniform non-leaky 404). RBAC matrix in auth/rbac.py (tenant_admin/editor/member/viewer; service accounts internal-only; 403 forbidden; default-tenant compatibility mode when OIDC unconfigured). Gates independently re-verified: ruff/format/mypy clean, pytest 646 passed / 11 deselected. Spec: new backend/auth-tenancy.md. Docs: identity-tenants.md runbook + gateway doc section. No gateway defect; no issue filed.

### Git Commits

| Hash | Message |
|------|---------|
| `5dcd7ce` | feat(auth): gateway identity, tenant isolation, and RBAC |

### Status

[OK] **Completed**


## Session 20: Document summary/associations SSE endpoints
<!-- trellis-session: v=2 fp=cacdd4d31413e351 -->

**Date**: 2026-09-15
**Task**: Document summary/associations SSE endpoints
**Branch**: `main`

### Summary

Planned and implemented 09-15-document-agent-sse. User resolved the PRD's open question in favor of typed progress events. Both document agent endpoints (POST /documents/{id}/summary and /associations) now stream SSE via a new shared serializer (api/v1/endpoints/sse.py; chat delegates unchanged) and schemas/agent_stream.py event contract (run_started, summary_progress per map/reduce pass, summary, associations, done; chat ErrorEvent reused). Services gained stream generators with 404 gate pre-first-yield (pre-stream 404 envelopes preserved), cache-hit shortcut with no model call, and single terminal error events; coroutine methods kept as draining wrappers. Offline tests via shared parse_sse cover success/cache/error/multi-pass/no-candidate paths; gates independently re-verified: ruff/format/mypy clean, pytest 661 passed / 11 deselected. spec(backend)/error-handling.md documents the agent SSE contract. No defects found by trellis-check.

### Git Commits

| Hash | Message |
|------|---------|
| `a16d933` | feat(api): stream document summary and associations over SSE |

### Status

[OK] **Completed**


## Session 21: Gateway v1.2 integration and defect filing
<!-- trellis-session: v=2 fp=52c7b01763d325f9 -->

**Date**: 2026-09-16
**Task**: Gateway v1.2 integration and defect filing
**Branch**: `main`

### Summary

Completed 09-16-gateway-v1-2-integration per docs/gateway-v1.2-integration.md §7 checklist. Gateway schema upgraded 3→4 (dirty=false), stack rebuilt healthy, key lifecycle verified. Capability matrix verified (only seed gateway-echo; no self-built models so no §2.1 UPDATE targets). Fake-mode smoke reached terminal done but exposed a genuine gateway defect: streamed tool-call deltas lack function.name/id so the server tool loop cannot dispatch (non-streaming correct) — filed lennylin-lab/knowledge-base-gateway#2. Real-model e2e blocked: only fake providers configured in gateway. 429 verification passed (rate_limit_exceeded + Retry-After: 46) with policy restored and independently DB-confirmed (120/8/1000000). Zero server code changes; offline baseline 660 passed / 11 deselected / 1 environmental failure (user .env OIDC vars leak into test fixtures — candidate test-isolation follow-up). Spec: chat-guidelines gateway adapter convention updated for v1.2 tools passthrough with streaming-defect caveat. Follow-ups: gateway #2 fix + real-provider rerun of stages 3-4; test isolation task.

### Git Commits

| Hash | Message |
|------|---------|
| `1f63d21` | docs(gateway): record v1.2 integration evidence; update adapter spec |

### Status

[OK] **Completed**


## Session 22: Gateway issue #2 retests passed; real-model e2e acceptance done
<!-- trellis-session: v=2 fp=1bb6748bc785a20e -->

**Date**: 2026-09-16
**Task**: Gateway issue #2 retests passed; real-model e2e acceptance done
**Branch**: `main`

### Summary

Retested gateway issue #2 across two fix iterations (no Trellis task, per user choice). First retest of 5bd165b: identity present but repeated in every args delta -> tool name concatenated server-side, dispatch still failed; findings posted as sanitized issue comment. Second retest of b887664: name/id only on opening fragment; openai SDK accumulation verified correct; server e2e tool loop reached terminal done. Then completed the deferred v1.2 real-model e2e: created gpt-5.5 catalog/provider/route/policy rows in gateway DB (env OPENAI key initially INVALID_API_KEY, user fixed), probe server via gateway with KB_CHAT_MODEL=gpt-5.5. Turn 1: run_started -> tool_call_started -> sources(4 docs) -> tool_call_finished(success) -> done, tool_calls=1, citations [1,2,3] within 4 sources. Follow-up same session: carried_sources=4, tool_calls=3, outcome=success. Key minted/revoked, probe server stopped, temp files shredded. Gateway v1.2 acceptance checklist now fully closed; spec adapter convention's tools caveat satisfied.

### Git Commits

| Hash | Message |
|------|---------|
| `1f63d21` | docs(gateway): record v1.2 integration evidence; update adapter spec |

### Status

[OK] **Completed**


## Session 23: All agent endpoints verified through gateway with real model
<!-- trellis-session: v=2 fp=85fa49b3700f541d -->

**Date**: 2026-09-16
**Task**: All agent endpoints verified through gateway with real model
**Branch**: `main`

### Summary

Extended real-model gateway verification (KB_CHAT_MODEL=gpt-5.5, probe server on 8010) from chat to the remaining agent endpoints, all over SSE: (1) POST /documents/{id}/summary — run_started, 3 summary_progress (map passes + reduce), summary payload (document_id/summary/model/latency_ms), done; (2) POST /documents/{id}/associations — run_started, associations payload with candidate metadata (title/tags/reason/signal incl. cosine distance + shared tags), done; (3) POST /writing/suggest — run_started, status, answer deltas with a correct technical rewrite, done. No UTF-8 corruption anywhere (fake-only issue #3 does not affect real adapters). Writing first attempt returned 400 validation_failed for missing draft field — correct pre-stream envelope behavior, retried with valid body. Cleanup: probe server stopped, gateway key revoked, temp captures shredded. Journal recorded; no Trellis task per user choice.

### Git Commits

| Hash | Message |
|------|---------|
| `6b4f678` | docs(spec): clear gateway streaming-tools caveat after real-model e2e |

### Status

[OK] **Completed**


## Session 24: Gateway v1.3 model control plane adopted; deferred items closed via #5/#6/#7/#8 retests
<!-- trellis-session: v=2 fp=f20202c3e92db58e -->

**Date**: 2026-09-16
**Task**: Gateway v1.3 model control plane adopted; deferred items closed via #5/#6/#7/#8 retests
**Branch**: `main`

### Summary

Planned and completed 09-16-gateway-v1-3-integration (user approved typed plan). Server: llm/discovery.py (model facts, optional KB_CHAT_MODEL with startup default-model probe and fail-fast), empty-string normalization for CHAT_MODEL and EMBEDDING_DIM, llm/profile.py retrieval-profile overrides at the _build_retriever choke point with per-item source logging, embedding dim discovery threaded into provider+cache, 24 offline tests (684 passed / 1 pre-existing env failure). Ops: gateway schema v5, embeddings capability declared, default-model slots set, /v1/embeddings smoke. Deferred items closed across retests after gateway fixes: #5 per-provider credentials verified (tumuer Qwen3 embeddings upstream with own key), #6 catalog-injected dimensions verified (1536-wide real vectors), #7 multi-row default backfill fix verified, #8 min-of-declared ceilings verified live (cross-row 5/min proof, restored). Flakiness root-caused to local transparent-proxy network resets on gateway-container upstream connections (corrected earlier vendor-rejection hypothesis; direct pydantic-ai tools+stream works). Remaining externals: chat vendor intermittent tools+stream rejections; suggestion to route gateway container outside fake-ip proxy. Spec: gateway model control plane convention added to backend/chat-guidelines.md.

### Git Commits

| Hash | Message |
|------|---------|
| `ef63ba1` | feat(llm): adopt gateway v1.3 model control plane |
| `cdf4ec3` | chore(task): retest deferred v1.3 items after gateway per-provider creds |
| `cfc642c` | fix(config): empty KB_EMBEDDING_DIM means discover-from-gateway |
| `ce1e75f` | docs(task): correct v1.3 flakiness root cause to local proxy network resets |
| `ab520a0` | docs(task): verify gateway #8 min-of-declared ceilings live |

### Status

[OK] **Completed**


## Session 25: Draft endpoint SSE streaming
<!-- trellis-session: v=2 fp=1868736766f118b7 -->

**Date**: 2026-09-17
**Task**: Draft endpoint SSE streaming
**Branch**: `main`

### Summary

Converted POST /operations/draft from sync JSON to SSE (run_started -> draft -> done, one terminal error). Terminal operation state committed before terminal event; priming pattern keeps 404/503 as JSON; failed ops stay resumable; no auto-apply. Added additive OperationDraftEvent, scripted_draft_model fake, and full success/failure/pre-stream test coverage. All gates pass (ruff, strict mypy, 688 tests; one pre-existing env-only RBAC failure unrelated). Spec updated with draft event order + persistence-timing rule.

### Git Commits

| Hash | Message |
|------|---------|
| `9be2b39` | feat(operations): stream POST /operations/draft as SSE |
| `ea841d0` | docs(spec): add draft SSE event order and persistence-timing rule to error-handling |

### Status

[OK] **Completed**


## Session 26: Gateway issue #4 closed: per-model feature isolation completed
<!-- trellis-session: v=2 fp=839c07fac5efffd6 -->

**Date**: 2026-09-17
**Task**: Gateway issue #4 closed: per-model feature isolation completed
**Branch**: `main`

### Summary

Closed gateway issue #4. Exploration showed Feature 4's mechanism (capability matrix + central admit() gating pre-provider) already existed; the task was close-out. Gateway commit a9e961a: CapabilityError names failed capability+protocol in capability_not_supported messages (envelope frozen, golden/replay fixtures unchanged); new docs/capabilities.md authoritative reference (14 keys, defaults, gated surfaces, 6-step extension protocol, vision/reasoning reserved, retrieval_profile as non-gated catalog attribute); policy.Resolver.LimitsFor(subject, model) quota seam reserved for per-model overrides (behavior-identical, #8 min-fold untouched). Gates: gofmt/vet clean, go test 20 pkgs ok, -race ok, replay 12/12. Independent check verified envelope freeze, LimitsFor equivalence, doc-vs-struct accuracy, server-side contract consistency. Issue #4 closed with feature-evidence mapping (Features 1-3 shipped in v1.3 and adopted server-side). Note: max_tools violation intentionally remains validation_error (pre-existing).

### Git Commits

| Hash | Message |
|------|---------|
| `636bc00` | chore(task): gateway feature-4 close-out complete; issue #4 closed |

### Status

[OK] **Completed**
