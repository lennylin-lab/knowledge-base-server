# Design: Vector rescue as a lexical-failure backstop

Scopes the vector rescue tier (introduced in task 09-05, on-domain-gated in
task 09-10-irrelevant-query-noise-gates) so it fires only when the BM25 leg has
no surviving matches. Minimal, source-local coupling; no changes to primary-tier
gating, BM25 gating, fusion, or the relative floor.

## Current rescue precondition (`rag/retriever.py::filter_vector_rows_with_rescue`)

```
rescue fires when:  primary tier empty
                    AND rows exist
                    AND rescue not disabled (margin>0 and cap>0)
                    AND leg_min <= rescue_trigger_max_distance   # 09-10
```

The tier has no knowledge of the BM25 leg. For `q=python` the BM25 leg already
ranks the 6 genuine chunks, but rescue still fires (leg_min 0.586 ≤ 0.62) and
admits the whole ≤0.736 programming cluster, injecting 4 vector-only leaks that
survive fusion (single-leg RRF score ~0.0154 vs the 0.35 relative floor cutoff
0.011).

## Change: add a BM25-empty precondition

Rescue becomes the semantic backstop for lexical failure:

```
rescue fires when:  primary tier empty
                    AND rows exist
                    AND rescue not disabled
                    AND leg_min <= rescue_trigger_max_distance
                    AND bm25_leg_empty                          # NEW
```

When the BM25 leg has ≥1 surviving hit, the emptied vector leg stays empty
(today's single-tier behavior), so fusion returns the clean BM25 ranking. When
BM25 is empty (true lexical failure / vocabulary mismatch), rescue fires exactly
as today, preserving the 09-05 recall intent and the 09-10 off-domain trigger.

### Why this is the right layer (vs Options B/C)

The genuine and leaked chunks **interleave in vector distance** (relevant Python
c0 at 0.687 is farther than the Vue leak at 0.660), so no vector-side threshold
separates them. The clean separator is cross-leg agreement, and the cheapest
expression of "BM25 already answered lexically" is "BM25 leg non-empty". This
fixes `q=python` by construction (rescue suppressed → vector leg empty → BM25's
6 exact hits fuse alone, zero leaks) without touching the global relative floor
(Option B, risks genuine single-leg tails) or building confidence-weighted RRF
(Option C, larger scope).

## Helper signature and semantics

`filter_vector_rows_with_rescue` gains one boolean kwarg so it stays a pure,
offline-testable function (no leg-orchestration inside the helper):

```python
def filter_vector_rows_with_rescue(
    rows, *,
    max_distance: float,
    rescue_margin: float,
    rescue_max_distance: float,
    rescue_trigger_max_distance: float,
    bm25_leg_empty: bool,            # NEW — rescue only when True
) -> tuple[list[ChunkRow], int]:
```

New guard, placed in the rescue branch after the primary tier empties, before
the on-domain/window computation (order among the rescue preconditions is
immaterial; put it first for a cheap early-out):

```
primary = [... <= max_distance ...]
if primary or not rows:
    return primary, 0
if not bm25_leg_empty:           # BM25 already answered → no semantic backstop
    return [], 0
if rescue_margin <= 0 or rescue_max_distance <= 0:
    return [], 0
measured_min = min(measured)
if measured_min > rescue_trigger_max_distance:
    return [], 0
window = min(measured_min + rescue_margin, rescue_max_distance)
...
```

Behavior notes:

- `bm25_leg_empty=True` reproduces the 09-10 behavior bit-for-bit (this task is
  a strict tightening only in the BM25-non-empty case). Existing rescue unit
  tests pass `True`.
- No new Settings field and no new `DEFAULT_*` constant → drift-guard untouched.
- The disable sentinels (`margin<=0`/`cap<=0`, `trigger>=2.0`, `max_distance>=2.0`)
  are unchanged and still short-circuit.

## Caller wiring (`Retriever.retrieve`)

The retriever already computes `kept_es_hits` (BM25 survivors after
`filter_es_hits`) before the vector gate. Pass the emptiness flag:

```python
kept_es_hits = filter_es_hits(es_hits, min_score=self._bm25_min_score)
kept_vector_rows, vector_rescued = filter_vector_rows_with_rescue(
    vector_leg.rows,
    max_distance=self._vector_max_distance,
    rescue_margin=self._vector_rescue_margin,
    rescue_max_distance=self._vector_rescue_max_distance,
    rescue_trigger_max_distance=self._vector_rescue_trigger_max_distance,
    bm25_leg_empty=(len(kept_es_hits) == 0),        # NEW
)
```

`filter_es_hits` already runs before the vector gate today, so no reordering is
needed. "Empty" = zero surviving hits after the BM25 gate (Q1 decision: no count
threshold — avoids a new scale-dependent knob).

Concurrency note: both legs are gathered before this point (`asyncio.gather`),
so the BM25 survivor count is available synchronously here — no extra awaits,
no leg-ordering change.

## Data flow (unchanged shape)

`... gather(search_chunks, _vector_leg) → filter_es_hits → (bm25_leg_empty) →
filter_vector_rows_with_rescue → fuse_rrf → apply_relative_score_floor → hydrate`.
The only new edge is `kept_es_hits → vector gate`; no new module, no layering
change (rescue logic stays in `rag/`).

## Observability

No schema change. `vector_rescued` now reports 0 whenever the BM25 leg is
non-empty (correct: nothing was rescued). `vector_gated` keeps meaning "dropped
by the vector gate". `search_executed` fields unchanged.

## Edge cases

| Case | Behavior |
|------|----------|
| BM25 non-empty, primary vector empty, leg_min ≤ trigger | Rescue suppressed → vector leg empty → BM25-only fusion (the `q=python` fix) |
| BM25 empty, primary empty, leg_min ≤ trigger | Rescue fires as today (vocab-mismatch recall preserved) |
| BM25 empty, primary empty, leg_min > trigger | Off-domain → empty (09-10 trigger; unchanged) |
| Primary vector non-empty | Rescue never runs (unchanged); BM25 flag irrelevant |
| Vector leg degraded (`ran=False`, no rows) | No rescue regardless (unchanged) |
| GATES_OFF (`max_distance>=2.0`) | Primary keeps everything; rescue unreachable (unchanged) |
| BM25 leg degraded/error | Errors already propagate before gating; not reached |

## Tradeoffs / rejected

- **Option B — raise `SEARCH_RRF_MIN_RELATIVE` (~0.5)**: exploits the ~2× fused
  gap for `q=python`, one constant, but global and blunt — a single strong
  two-leg hit inflates the top and can cut genuine single-leg tails in mixed
  result sets. Rejected.
- **Option C — confidence-weighted RRF / rerank**: most general (down-weights
  single-leg rescued hits), but a larger fusion redesign. Deferred.
- **Tighten rescue window/trigger**: cannot work — relevant/irrelevant
  interleave in vector distance (see PRD table). Rejected.
- **Count threshold for "BM25 weak" (≤ N)**: introduces a scale-dependent knob;
  "zero survivors" is scale-free and matches the lexical-failure framing.

## Compatibility / rollback

- Backward compatible: passing `bm25_leg_empty=True` unconditionally restores
  the exact 09-10 behavior. No Settings field, no migration, no reindex.
- Rollback: single revert, or wire `bm25_leg_empty=True` at the call site.

## Files touched (expected)

| File | Change |
|------|--------|
| `rag/retriever.py` | `filter_vector_rows_with_rescue` gains `bm25_leg_empty`; new guard; `retrieve()` passes `len(kept_es_hits) == 0` |
| `tests/test_search_gates.py` | Unit: rescue suppressed when `bm25_leg_empty=False`; fires when `True`; existing cases updated to pass the kwarg |
| `tests/test_retriever.py` | Integration: BM25-non-empty + emptied primary vector → 0 rescued / no vector-only leaks; BM25-empty vocab-mismatch → rescued |
| `tests/test_search_api.py` | (Optional) `q=python`-style smoke if a representative fixture supports it |
