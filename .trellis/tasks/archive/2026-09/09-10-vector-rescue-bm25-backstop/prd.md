# In-domain keyword leaks topical neighbors: gate vector rescue on BM25-leg emptiness (lexical-failure backstop)

## Goal

A legitimate in-domain keyword query must not return topically-adjacent but
off-topic documents solely because dense embeddings cluster all programming
content together. Concretely: `q=python` must stop returning Vue 3 / Flutter /
PostgreSQL chunks while keeping every genuine Python/FastAPI chunk. Achieve this
by scoping the vector **rescue** tier to what it was designed for — a
*lexical-failure backstop* — so it does not fire when the BM25 leg already has
solid matches.

## Background — diagnosis (measured, real 15-doc corpus)

`GET /api/v1/search?q=python&limit=10` returned 10 items; 4 are irrelevant
leaks. Per-item audit fields (`es_rank`/`es_score`/`vector_rank`/
`vector_distance`) prove the mechanism:

| vector_rank | vector_distance | doc | verdict | legs |
|---|---|---|---|---|
| 1 | 0.586 | FastAPI c1 | relevant | both |
| 2 | 0.593 | FastAPI c2 | relevant | both |
| 3 | 0.610 | FastAPI c0 | relevant | both |
| 4 | 0.634 | Python c2 | relevant | both |
| **5** | **0.660** | **Vue 3 c1** | **LEAK** | vector only (`es_rank=null`) |
| **6** | **0.661** | **Vue 3 c0** | **LEAK** | vector only |
| 7 | 0.683 | Python c1 | relevant | both |
| **8** | **0.683** | **Flutter c2** | **LEAK** | vector only |
| **9** | **0.686** | **PostgreSQL c0** | **LEAK** | vector only |
| 10 | 0.687 | Python c0 | relevant | both |

Findings:

1. **The leaks came in via the vector RESCUE tier, not the primary tier and not
   BM25.** `leg_min = 0.586 > 0.45` (primary tier empties) and `≤ 0.62` (the
   09-10 on-domain trigger fires), so rescue fired with window
   `min(0.586 + 0.15, 0.85) = 0.736`, admitting the entire programming cluster
   ≤ 0.736.
2. **The vector leg cannot self-separate.** Genuine Python chunks (0.683, 0.687)
   sit *farther* than the Vue leak (0.660); relevant and irrelevant interleave
   in distance space, so no rescue-window tightening cleanly separates them
   without dropping genuine chunks.
3. **The only clean separator is cross-leg agreement.** All 6 relevant chunks
   have a BM25 rank (exact-term match, `es_score` 4.7–13.7); all 4 leaks have
   `es_rank = null`.
4. **For this query the rescue tier is pure harm.** BM25 alone already ranks
   exactly the 6 genuine chunks; if rescue had not fired, the vector leg would
   be empty and fusion would return the clean BM25 result with zero leaks.

Root-cause framing: the rescue tier's purpose (task 09-05) is to restore
semantic recall when short keyword queries sit far from long chunks. That is a
backstop for **lexical failure**. When the BM25 leg already has solid lexical
matches, the semantic backstop is unnecessary and only injects the broad
same-domain cluster.

## Requirements

### R1 — Rescue is a lexical-failure backstop

- The vector rescue tier must fire ONLY when the BM25 leg produced no surviving
  matches (after its own gates). When BM25 has matches, the emptied vector leg
  stays empty (today's single-tier behavior for that leg).
- The existing rescue preconditions still apply and compose: rescue fires only
  when the primary vector tier is empty AND the leg is on-domain
  (`leg_min ≤ SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE`) AND now additionally
  the BM25 leg is empty.

### R2 — Preserve genuine lexical-failure recall

- A true vocabulary-mismatch short keyword query (BM25 leg empty, on-domain
  semantic neighbors present) must still be rescued exactly as today.
- The five out-of-domain queries from the archived task 09-10-irrelevant-query-
  noise-gates must still return 0 items (BM25 empty + off-domain leg_min > 0.62
  → trigger already blocks rescue; the new BM25 condition does not regress
  this).

### R3 — No regressions to existing behavior/contracts

- Primary-tier vector behavior, BM25 gating, RRF fusion, and the relative floor
  are unchanged. This task does NOT raise `SEARCH_RRF_MIN_RELATIVE` or introduce
  confidence-weighted RRF (both considered and rejected below).
- Degradation paths (`ran=False`), tag filtering, and GATES_OFF behavior
  unchanged.
- `search_executed` observability stays consistent (counts only, no query text;
  `vector_rescued` keeps its meaning — now 0 whenever BM25 is non-empty).
- Config↔`DEFAULT_*` drift-guard stays green.

## Non-goals

- Raising the RRF relative floor (Option B) — global, blunt, risks cutting
  genuine single-leg tails in mixed result sets.
- Confidence-weighted RRF / cross-encoder rerank (Option C) — larger design;
  out of scope.
- Re-tuning `SEARCH_VECTOR_MAX_DISTANCE` (0.45) or the rescue window/trigger
  from task 09-10 — unchanged.

## Acceptance Criteria

- [ ] AC1: `q=python&limit=10` (real corpus) returns only Python/FastAPI chunks;
      the Vue 3 / Flutter / PostgreSQL chunks no longer appear.
- [ ] AC2: With a non-empty BM25 leg, `filter_vector_rows_with_rescue` (or its
      caller) admits 0 rescued rows — `vector_rescued == 0` — even when the
      primary tier is empty and `leg_min ≤ trigger`. Unit-tested.
- [ ] AC3: With an empty BM25 leg, on-domain short-keyword rescue still fires
      exactly as before (recall preserved). Unit-tested.
- [ ] AC4: The five archived out-of-domain queries still return 0 items (no
      regression of task 09-10). Covered by existing/extended tests.
- [ ] AC5: Full offline suite + `ruff` + `mypy` clean; drift-guard green.

## Open questions

- Q1: Define "BM25 leg empty" — recommended: zero surviving BM25 hits after
  `filter_es_hits` (the gated survivor count the retriever already computes). A
  count threshold (e.g. "≤ N") is intentionally avoided to prevent a new
  scale-dependent knob unless a real case demands it.
- Q2: Where to place the coupling — the decision needs both leg results, which
  the retriever already has in `retrieve()` before fusion; the pure helper can
  take a boolean flag to stay unit-testable (see design).
