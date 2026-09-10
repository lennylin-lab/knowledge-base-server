# Irrelevant queries return noise: vector rescue admits noise band, BM25 identity group bypasses coverage gate

## Goal

Out-of-domain (completely irrelevant) queries must return **empty** results,
not large result sets. Today they return 14–32 chunks because two independent
relevance-gate holes each admit noise. Close both holes without regressing the
recall the gates were previously tuned to protect (short in-domain keyword
recall from task 09-05; genuine prose/identifier recall).

## Background (observed, from the issue)

Corpus: 15 Chinese programming-tech documents (37 chunks). Five out-of-domain
queries via `GET /api/v1/search?q=...&limit=50`:

| Query (irrelevant) | Hits | Vector distance range | BM25 leg |
| --- | --- | --- | --- |
| 红烧肉怎么做才好吃 | 28 | 0.715 – 0.849 | 0 |
| 量子力学薛定谔的猫 | 32 | 0.655 – 0.778 | 6 (abnormal) |
| 恐龙为什么灭绝了 | 23 | 0.675 – 0.820 | 0 |
| 感冒了应该吃什么药 | 15 | 0.773 – 0.831 | 0 |
| 世界杯足球赛冠军 | 14 | 0.774 – 0.848 | 0 |

Contrast — relevant queries ("Redis 分布式锁", "MySQL 事务隔离") score
0.38 – 0.45 cosine distance (inside the primary vector ceiling).

Two independent root causes:

1. **Vector rescue tier admits the whole noise band.** When the primary
   ceiling (`SEARCH_VECTOR_MAX_DISTANCE=0.45`) empties the vector leg, the
   rescue tier admits rows within `min(leg_min + 0.15, 0.85)`. For
   out-of-domain queries `leg_min ≈ 0.65–0.8`, so the window widens to ~0.85
   (the cap) and admits up to 32 chunks. The unrelated-query distance band
   (0.65–0.85) lies *inside* the cap, so the cap never gates it.
   (`src/app/rag/retriever.py::filter_vector_rows_with_rescue`)
2. **BM25 term-coverage gate does not constrain the identity group.**
   `minimum_should_match: 70%` applies only to the `chunk_text` leaf; the
   `title`/`heading_path` `best_fields` group is unconstrained under the outer
   `minimum_should_match: 1`. A single ubiquitous function word ("的") matching
   in `title` activates the whole BM25 leg with a high score (title-field IDF
   of "的" is 2.72 because only 2 of 37 titles contain it), outranking genuine
   prose evidence in fusion. (`src/app/search/queries.py::bm25_chunk_query`)

RRF's relative floor (`SEARCH_RRF_MIN_RELATIVE=0.35`) cannot rescue precision
here: with a single leg alive, fused scores are `1/(60+rank)`; every rank up to
~111 passes 35% of the top score, so nothing is dropped before the `limit`
slice. The relative floor is NOT a root cause and must not be repurposed as a
patch.

## Requirements

### R1 — Vector rescue must stay silent for out-of-domain legs (test-first)

- The rescue tier must NOT admit rows when the vector leg is not plausibly
  on-domain. Concretely: an "on-domain trigger" gates rescue so a leg whose
  minimum distance is already in the out-of-domain band stays empty.
- The trigger threshold value MUST be calibrated against the **real 15-doc
  corpus** via a live embedding probe BEFORE any production wiring is
  finalized. It must not be guessed. The 09-05 toy-corpus calibration
  (`缓存`=0.560, `锁`=0.483) conflicts with a naive 0.55 threshold, so
  real-corpus short-keyword distances must be measured first.
- Preserve the 09-05 policy intent: genuinely short in-domain keyword queries
  that land above the 0.45 primary ceiling but are still on-domain must keep
  being rescued (recall-first for real matches).
- Prefer expressing intent via an on-domain trigger anchored to `leg_min`
  rather than hard-binding a magic distance to this embedding model; keep the
  existing absolute cap as a backstop.

### R2 — BM25 identity group must respect term coverage

- The `title`/`heading_path` `multi_match` group must not be activatable by a
  single stopword/function-word match in a multi-token query.
- Single-term identifier searches (e.g. `redis`) must still match (a one-term
  query is unaffected by percentage coverage).
- Prefer a query-layer fix (no reindex): a `minimum_should_match` on the
  identity `multi_match`, consistent with the prose leaf's coverage semantics.

### R3 — No regressions

- Relevant in-domain queries (short keyword and natural-language) keep their
  current recall and ranking.
- Degradation paths (`ran=False`), tag filtering, and the GATES_OFF fixture
  behavior are unchanged.
- `search_executed` observability stays consistent (no query text; counters
  keep their meaning; any new counter is count-only).
- Existing drift-guard between `core/config.py` Settings and `retriever.py`
  `DEFAULT_*` constants stays green.

## Non-goals

- Re-tuning `SEARCH_VECTOR_MAX_DISTANCE` (0.45) globally — it is well
  calibrated for long queries; changing it reintroduces long-query noise.
- Confidence-weighted RRF or changing the RRF relative floor — out of scope;
  the two root causes are gated at their source.
- Mapping/analyzer changes requiring a reindex (e.g. title stopword filter) —
  rejected in favor of the query-layer coverage fix.

## Acceptance Criteria

- [ ] AC1: For the five out-of-domain queries in the table, hybrid retrieval
      returns **0 items** (both legs empty after gating) at `limit=50`.
- [ ] AC2 (calibration recorded): a live probe against the real 15-doc corpus
      has measured short in-domain keyword distances vs out-of-domain query
      distances, and the chosen rescue on-domain trigger value is recorded in
      `design.md` with the measured evidence. A clean separating threshold is
      confirmed, or (if bands overlap) the precision/recall tradeoff is
      documented and explicitly decided.
- [ ] AC3: With the calibrated trigger, a genuine short in-domain keyword query
      whose `leg_min` sits between 0.45 and the trigger is still rescued
      (recall preserved) — covered by a unit test on
      `filter_vector_rows_with_rescue`.
- [ ] AC4: `bm25_chunk_query` emits a `minimum_should_match` on the identity
      `multi_match`; a 4-token query with one stopword title hit does not
      activate the identity group, while a single-term identifier query still
      matches — covered by `tests/test_es_queries.py`.
- [ ] AC5: For "量子力学薛定谔的猫" the BM25 leg returns 0 hits after the
      identity coverage fix.
- [ ] AC6: All existing offline tests pass, including the config↔retriever
      drift-guard; `ruff` + `mypy` clean.

## Open questions (to resolve during planning/execution)

- Q1: Does a clean on-domain trigger threshold exist on the real corpus, or do
      in-domain short keywords and out-of-domain queries overlap in distance
      space? (Resolved by the R1 probe; drives whether R1 is a pure gate fix or
      a policy tradeoff.)
- Q2: Exact coverage percentage for the identity group — reuse `70%` (prose
      parity) unless the probe/tests indicate otherwise.
