# Design: Close the two irrelevant-query noise-gate holes

Extends the gate architecture from tasks 09-05 (short-query vector rescue) and
09-08 (BM25 scoring / term coverage). Two independent, source-local fixes; no
cross-cutting changes to fusion or the relative floor.

## Hole 1 — Vector rescue on-domain trigger

### Current mechanism (`rag/retriever.py::filter_vector_rows_with_rescue`)

```
primary tier: distance <= max_distance (0.45)
  survivors → fusion (unchanged)
  empty      → rescue tier:
                 window = min(leg_min + rescue_margin, rescue_max_distance)
                 rows with distance <= window → fusion (flagged rescued)
```

The rescue fires **whenever the primary tier is empty**, and the only ceiling
is `rescue_max_distance` (0.85). For an out-of-domain leg (`leg_min ≈ 0.715`)
the window becomes `min(0.865, 0.85) = 0.85`, admitting the entire noise band.
The cap was intended as the noise floor, but the unrelated-query band
(0.65–0.85) sits inside it, so it never gates.

### Change: add an on-domain trigger

Introduce a new gate condition: rescue fires only when the leg is *plausibly
on-domain*, defined by its own closest hit:

```
empty primary AND leg_min <= rescue_trigger_max_distance
  → rescue tier as today (window = min(leg_min + margin, cap))
else
  → leg stays empty  (empty beats noise)
```

Rationale for anchoring the trigger to `leg_min` (rather than only lowering the
cap):

- **Semantics**: "is the closest chunk even remotely relevant?" is a clearer
  contract than a magic per-row distance ceiling. The adaptive window
  (`leg_min + margin`) already handles *how wide* to rescue; the trigger adds
  *whether* to rescue at all.
- **Model-portability**: lowering `rescue_max_distance` to ~0.60 also works
  numerically (window is capped below the noise band so nothing is admitted),
  but it hard-binds the cap to this embedding model's distance distribution and
  couples "how wide" with "whether". The trigger keeps the cap as a pure
  backstop for pathological low-min/long-tail legs.

Keep `rescue_max_distance` (0.85) as the absolute backstop; it is unchanged.

### New Settings field

`core/config.py`, search relevance gates block:

| Field | Type | Default (starting point) | Meaning | Disable |
|-------|------|--------------------------|---------|---------|
| `SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE` | `float` | **0.62** (from R1 probe, below) | Rescue fires only when `leg_min <= this`; above it the leg stays empty | `>= 2.0` disables the trigger (rescue fires whenever primary is empty — pre-task behavior) |

Disable sentinel mirrors `filter_vector_rows`'s `>= 2.0` convention (cosine
distance max). Default value is deliberately **left TBD in this design until the
R1 live probe runs** — see Calibration section. A drift-guard-safe placeholder
constant `DEFAULT_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE` is added to
`rag/retriever.py` mirroring the Settings default.

### Helper signature change

`filter_vector_rows_with_rescue` gains one kwarg:

```python
def filter_vector_rows_with_rescue(
    rows, *,
    max_distance: float,
    rescue_margin: float,
    rescue_max_distance: float,
    rescue_trigger_max_distance: float,   # NEW
) -> tuple[list[ChunkRow], int]:
```

New early-exit after the primary tier empties and before computing the window:

```
measured_min = min(measured)
if measured_min > rescue_trigger_max_distance:   # off-domain leg
    return [], 0
window = min(measured_min + rescue_margin, rescue_max_distance)
```

`rescue_trigger_max_distance >= 2.0` → the check never trips (backward compatible
with the pre-task single-condition rescue). Existing disable paths
(`rescue_margin <= 0` or `rescue_max_distance <= 0`) are unchanged and still
short-circuit first.

### Wiring

- `Retriever.__init__`: new kwarg `vector_rescue_trigger_max_distance` (default
  = new constant); passed into `filter_vector_rows_with_rescue`.
- `api/deps.py::_build_retriever`: inject
  `settings.SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE` (single wiring point).
- `.env.example`: add the new setting with a comment.
- Drift-guard test: extend the Settings↔`DEFAULT_*` mirror.

### Observability

No new `search_executed` field is required — `vector_rescued` already reports
how many rows the rescue admitted (now 0 for out-of-domain legs, which is the
desired signal). `vector_gated` keeps meaning "dropped by the vector gate".

## Hole 2 — BM25 identity group term coverage

### Current (`search/queries.py::bm25_chunk_query`)

```python
{"multi_match": {"query": q, "fields": _IDENTITY_FIELDS, "type": "best_fields"}}
```

No `minimum_should_match`; a single "的" token matching `title` satisfies the
outer `minimum_should_match: 1` and activates the whole leg.

### Change

Add `minimum_should_match` to the identity `multi_match`, threaded from the same
`min_coverage` the prose leaf uses (default `70%`):

```python
identity_match: dict[str, Any] = {
    "query": q,
    "fields": _IDENTITY_FIELDS,
    "type": "best_fields",
}
if min_coverage:
    identity_match["minimum_should_match"] = min_coverage
...
{"multi_match": identity_match},
```

Behavior:

- `best_fields` generates a per-field `match`; `minimum_should_match` applies
  within each field. ES rounds the percentage **down**: a 4-token query needs
  `floor(4 * 0.7) = 2` terms in `title` (or in `heading_path`). "量子力学薛定谔
  的猫" → only "的" in title → 1 < 2 → identity group fails. ✅
- Single-term queries (`redis`, 1 term) are unaffected by percentage coverage
  (the lone term stays required), so identifier searches still match 100%. ✅
- The `chunk_text.code` group is intentionally left without coverage (its
  analyzer keeps English stopwords IK drops — a percentage means something
  different there), matching the existing rationale for the prose-only gate.

### Config

Reuse `SEARCH_BM25_MIN_COVERAGE` (already threaded to `bm25_chunk_query` as
`min_coverage`). No new setting. An empty `min_coverage` still omits the key
everywhere (identity group reverts to unconstrained), keeping the gate-disable
contract uniform across leaves.

## Data flow (unchanged shape)

`router → SearchService → Retriever.retrieve → {bm25_chunk_query + search_chunks,
_vector_leg} → filter_es_hits + filter_vector_rows_with_rescue → fuse_rrf →
apply_relative_score_floor → hydrate`. Both fixes live inside existing
functions; no layering or contract changes (rescue stays in `rag/`,
query-building stays in `search/`).

## Calibration (R1) — measured 2026-09-10

Probe: `tests/test_vector_distance_probe.py::test_probe_real_corpus_on_domain_vs_off_domain`
(live_llm + es; run `uv run pytest -m live_llm tests/test_vector_distance_probe.py -s`).
Real dev corpus `kb_documents` (15 docs / 37 chunks), model `Qwen/Qwen3-Embedding-4B`
(1536d), chunk inputs reconstructed via `embedding_input`. Validity check: the
fresh-embedded off-domain leg_min values reproduce the PRD's production-observed
distance ranges to ±0.001 (量子力学 0.656 vs 0.655; 红烧肉 0.714 vs 0.715; 感冒
0.773 vs 0.773; 世界杯 0.774 vs 0.774) — embedding drift on this stack is
negligible, so the bands below are production-faithful.

| Query | leg_min | argmin chunk | class |
|---|---|---|---|
| redis | 0.473 | Redis / 缓存三大问题 | in-domain keyword |
| 缓存 | 0.548 | Java 核心知识点 / 常见面试要点 | in-domain keyword |
| 锁 | 0.603 | Java 核心知识点 / 并发编程 | in-domain keyword |
| 分布式 | 0.613 | Flutter / 性能优化清单 | in-domain keyword |
| 索引 | 0.616 | PostgreSQL/MySQL 差异对比 | in-domain keyword |
| 事务 | 0.652 | Spring Boot / 事务管理 | in-domain keyword |
| 量子力学薛定谔的猫 | 0.656 | Java 核心知识点 / 常见面试要点 | OFF-domain |
| 恐龙为什么灭绝了 | 0.674 | Java 核心知识点 / 常见面试要点 | OFF-domain |
| 红烧肉怎么做才好吃 | 0.714 | React / Hooks | OFF-domain |
| 感冒了应该吃什么药 | 0.773 | FastAPI / 与 Django、Flask 取舍 | OFF-domain |
| 世界杯足球赛冠军 | 0.774 | Java 核心知识点 / 常见面试要点 | OFF-domain |

### Q1 resolution: the bands TOUCH — this is the overlap branch

Highest in-domain leg_min (事务 0.652) and lowest off-domain leg_min
(量子力学 0.656) sit 0.004 apart. A threshold between them (≈0.654) would be
overfit to this exact corpus+model snapshot — any model upgrade or corpus
growth flips it. Per PRD AC2, this requires an explicit, documented policy
decision rather than a "clean" separating value.

### Policy decision: precision-first — `SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE = 0.62`

- **Precision margin is real.** The nearest off-domain leg_min is 0.036 above
  the trigger, so AC1 (all five noise queries → 0 items) holds with drift and
  corpus-growth headroom instead of surviving on a knife edge.
- **Recall cost is exactly one probe keyword.** Bare `事务` (0.652) loses
  vector rescue. Its BM25 leg still fires (a single IK token passes the 70%
  coverage floor), so the query still returns results — only the vector
  leg's contribution is capped. All other probe keywords (redis 0.473,
  缓存 0.548, 锁 0.603, 分布式 0.613, 索引 0.616) stay rescued, preserving
  the 09-05 recall-first policy for short in-domain keywords.
- **Consistent with prior calibration.** 09-05's toy-corpus numbers (缓存
  0.560, 锁 0.483) and the real-corpus re-measurement (缓存 0.548, 锁 0.603)
  both sit below 0.62; the naive 0.55 threshold the PRD warned about would
  have lost 锁 (0.603) and 分布式/索引 — 0.62 keeps them.

Rejected alternatives: `0.65` (rescues the same set but sits only 0.006 below
the off-domain floor — no robustness gain over 0.654); `0.654` (additionally
rescues 事务 with a 0.002 margin — indefensible against model drift); `0.66+`
(admits the 量子力学 noise leg, fails AC1).

Final trigger value: **0.62** (policy decision recorded above; user-approved at
gate B, 2026-09-10).

## Tradeoffs / rejected

- **Lower `SEARCH_VECTOR_RESCUE_MAX_DISTANCE` only** (rejected as the primary
  fix): numerically closes hole 1 but hard-binds the cap to this model's
  distribution and conflates "whether" with "how wide". Kept as backstop only.
- **Title stopword analyzer** (rejected): needs a mapping change + full reindex;
  the query-layer coverage fix is lighter and symmetric with the prose gate.
- **Strengthen RRF relative floor** (rejected): not a root cause; risks
  regressing legitimate single-leg result sets.

## Compatibility / rollback

- Both changes are backward-compatible via existing disable sentinels:
  `SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE >= 2.0` restores pre-task rescue;
  empty `SEARCH_BM25_MIN_COVERAGE` restores the unconstrained identity group.
- Single revert; no DB migration, no reindex.

## Files touched (expected)

| File | Change |
|------|--------|
| `core/config.py` | New `SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE` field + docstring sentinel |
| `rag/retriever.py` | `DEFAULT_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE`, helper kwarg + on-domain check, `Retriever` kwarg, pass-through |
| `search/queries.py` | `minimum_should_match` on the identity `multi_match` |
| `api/deps.py` | Inject the new trigger setting |
| `.env.example` | New setting entry |
| `tests/test_vector_distance_probe.py` | Real-corpus in-domain vs out-of-domain distance probe (R1) |
| `tests/test_search_gates.py` | Rescue on-domain-trigger unit cases + drift-guard update |
| `tests/test_retriever.py` | Integration: out-of-domain leg empty; in-domain short keyword still rescued |
| `tests/test_es_queries.py` | Identity-group `minimum_should_match` assertions |
| `tests/test_search_api.py` | Out-of-domain query → 0 items smoke (if a fixture corpus supports it) |
