# Design: Search relevance quality gates

## Problem restatement

The retriever ranks chunks but never asks "is this chunk good enough?" Small
corpora + fixed `limit` ⇒ weak matches fill the tail. Fix: absolute and
relative quality gates before returning hits.

## Architecture (unchanged shape, new filter stages)

```
q ─┬─ ES BM25 ──> [(key, es_score)] ──> BM25 gate ──┐
   │                                                  ├─ RRF fuse ──> relative gate ──> hydrate ──> ≤limit
   └─ embed → pgvector ──> [(key, distance)] ──> vector gate ──┘
```

Gates sit **inside** `Retriever.retrieve()` after each leg returns, before
`fuse_rrf`, plus one post-fusion pass. No new services or endpoints.

## Settings (new `Settings` fields)

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `SEARCH_BM25_MIN_SCORE` | `float` | `1.0` | Drop ES hits with `_score` below this. `0.0` disables. |
| `SEARCH_VECTOR_MAX_DISTANCE` | `float` | `0.45` | Drop vector hits with cosine distance above this. `>= 2.0` disables (cosine distance ∈ [0, 2]). |
| `SEARCH_RRF_MIN_RELATIVE` | `float` | `0.35` | After fusion, keep hit `h` only if `h.score >= top.score * this`. `0.0` disables. |

Defaults target small corpora: prefer empty results over noise. Values are
starting points — calibrate against the seeded test corpus during
implementation; note final chosen defaults in a spec update if they shift.

Settings flow: `Settings` → `Retriever.__init__` (store thresholds) OR
read inside `retrieve` via injected settings snapshot — follow existing
`build_search_service` / deps wiring pattern (constructor injection keeps
the retriever testable without env).

## Leg-level gates

### BM25 (`search/es.py` + `rag/retriever.py`)

`EsChunkHit` already carries `score: float` from ES `_score`. Filter in the
retriever immediately after `search_chunks` returns:

```python
es_hits = [h for h in es_hits if h.score >= self._bm25_min_score]
```

Optional: also pass `min_score` into `bm25_chunk_query` so ES prunes early.
Prefer **both** when the default is non-zero: ES-side `min_score` reduces
work; Python-side filter keeps unit tests independent of ES score quirks.

### Vector (`repositories/document_chunk.py`)

Extend `search_similar` to SELECT and return cosine distance alongside
`ChunkRow` fields. Options:

- Add `distance: float` to `ChunkRow` (nullable — hydration reads omit it).
- Or introduce `SimilarChunkRow(ChunkRow fields + distance)`.

Prefer a dedicated `VectorChunkHit` / extend `ChunkRow` with optional
`distance: float | None` to avoid breaking hydration callers.

Filter in retriever after vector leg:

```python
rows = [r for r in rows if r.distance <= self._vector_max_distance]
```

Repository still fetches `CANDIDATE_POOL` rows then filters — same bound as
today; over-fetch is acceptable at pool=50.

## Post-fusion relative gate

Pure function in `rag/retriever.py`:

```python
def apply_relative_score_floor(hits: Sequence[FusedHit], *, min_relative: float) -> list[FusedHit]:
    if not hits or min_relative <= 0:
        return list(hits)
    top = hits[0].score  # already sorted desc
    if top <= 0:
        return []
    cutoff = top * min_relative
    return [h for h in hits if h.score >= cutoff]
```

Run after `fuse_rrf`, before `[:limit]` (or after limit slice — prefer
**before** limit so weak tail doesn't consume slots: fuse → relative gate →
`[:limit]`).

## Data model / API changes

### Internal

- `RetrievedChunk`: add `es_score: float | None`, `vector_distance: float | None`.
- `FusedHit` unchanged; leg scores attached during hydration mapping.

### `SearchHit` schema

Add optional fields (default `None`):

```python
es_score: float | None = None
vector_distance: float | None = None
```

Backward-compatible for API consumers (new keys only).

### Logging

Extend `search_executed` event with gate stats (counts before/after each
stage) — no query text. Example fields: `es_before`, `es_after`,
`vector_before`, `vector_after`, `fused_before`, `fused_after`.

## Wiring

- `Retriever.__init__`: accept threshold kwargs (defaults from Settings in
  `api/deps.py` `build_search_service` / retriever factory).
- Tests construct `Retriever(..., bm25_min_score=0, vector_max_distance=2.0,
  rrf_min_relative=0)` to disable gates when testing legacy behavior, or
  use explicit thresholds in new gate-focused tests.

## Edge cases

| Case | Behavior |
|------|----------|
| BM25-only mode | Only BM25 + relative gates apply |
| Vector leg degraded | Vector gate N/A; BM25 + relative gates apply |
| All ES hits below floor | ES leg empty; fusion may still use vector leg |
| Both legs empty after gates | Return `items=[]`, `hit_count=0` |
| Single hit after fusion | Relative gate keeps it (score == top) |
| Tag filter + gates | Tag filter unchanged (both legs); gates run after |

## Tradeoffs / rejected

- **Gate only at API layer** (rejected): chat agents would still ingest noise.
- **Document collapse** (rejected this task): product decision to defer.
- **Query-param thresholds** (rejected MVP): Settings-only keeps surface small.
- **RRF score as absolute threshold** (rejected): rank-derived scores are not
  comparable across queries; use leg raw scores + relative fused cutoff.

## Rollback

Single revert; no migration. Threshold defaults can be set to disable values
(`0.0` / `2.0`) if a hotfix is needed without code revert.

## Files touched (expected)

| File | Change |
|------|--------|
| `core/config.py` | Three new Settings fields |
| `search/queries.py` | Optional `min_score` in ES body |
| `search/es.py` | Pass-through unchanged |
| `repositories/document_chunk.py` | Return distance from `search_similar` |
| `rag/retriever.py` | Gates, relative floor helper, extended models |
| `services/search.py` | Map new fields to `SearchHit` |
| `schemas/search.py` | `es_score`, `vector_distance` on `SearchHit` |
| `api/deps.py` | Wire Settings into Retriever |
| `tests/*` | Gate units + retriever/search integration |

Agents (`qa.py`, `writing.py`) unchanged — they consume gated retriever output
via existing `RetrievedChunk` / source mapping if extended there too.
