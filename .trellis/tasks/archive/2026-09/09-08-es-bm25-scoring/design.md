# Design: ES BM25 scoring overhaul

## Problem restatement

The BM25 leg has the right fields but the wrong scoring. Four independent
mechanisms are wrong at once: the search-side analyzer emits index-time
artifacts (D2), the index stores duplicate tokens (D3), `best_fields`
discards cross-field evidence (D1), and the relevance gate is an absolute
score floor on a query-dependent scale (D4/D5/D6).

Fixes must respect two hard constraints: `heading_path` textually contains
`title` (C1), so additive scoring double-counts titles; and analyzers may
only be declared in the mapping (C3).

## Change 1 — split the `code` analyzer (R1, R2)

`word_delimiter_graph` with `preserve_original` + `catenate_words` is an
*index-time* expansion. Using it as the search analyzer stacks alternatives
at one position, and Lucene turns a stacked graph into a phrase query.

```
                      index analyzer                 search analyzer
ConnectionPool  ->  connectionpool, connection,  ->  connection, pool
                    pool                             (flat, plain OR)
```

`_CHUNK_SETTINGS.analysis.analyzer`:

| analyzer | tokenizer | filters | used at |
|----------|-----------|---------|---------|
| `code` | whitespace | `code_delimiter`, `flatten_graph`, `lowercase`, `remove_duplicates` | index |
| `code_search` | whitespace | `code_delimiter_search`, `lowercase` | search |

- `code_delimiter` — unchanged from today (`preserve_original`,
  `split_on_case_change`, `catenate_words`, `split_on_numerics=false`,
  `stem_english_possessive=false`).
- `code_delimiter_search` — same filter type with `preserve_original=false`
  and `catenate_words=false`. It only splits; it never stacks, so no
  `flatten_graph` is needed (and none should be added: flattening is an
  index-time requirement).
- `remove_duplicates` (ES built-in) drops the repeated same-position token
  that `preserve_original` + `catenate_words` produce for camelCase (D3). It
  runs after `lowercase` so casing variants collapse first.

Binding in `_CHUNK_MAPPINGS`:

```python
"chunk_text": {
    "type": "text",
    "analyzer": "ik_max_word",
    "search_analyzer": "ik_smart",
    "fields": {"code": {"type": "text", "analyzer": "code", "search_analyzer": "code_search"}},
}
```

Verified against the live node with `_analyze` (2026-09-08): the proposed
search filter chain yields `ConnectionPool -> [connection, pool]`,
`async_bulk -> [async, bulk]`, `setState -> [set, state]` — flat, one token
per position.

**Trade-off**: the exact identifier form is no longer a distinct query term,
so a chunk containing the words "connection" and "pool" separately scores
like one containing `ConnectionPool`. Accepted: the index still holds
`connectionpool`, recall is symmetric in both directions, and precision on
this corpus is dominated by the prose fields. The alternative —
`auto_generate_synonyms_phrase_query: false` on the query — was rejected
because it patches the symptom at the query site while leaving a search
analyzer that emits index-time artifacts, and it pushes analyzer knowledge
into `queries.py` against C3's spirit.

## Change 2 — additive cross-field shape with a max-within-group (R3)

Three evidence groups. Independent groups sum; the overlapping
title/heading group takes its max so C1 cannot double-count.

```python
{
  "bool": {
    "should": [
      # group 1: document identity. heading_path CONTAINS title, so max, not sum.
      {"multi_match": {"query": q, "fields": ["title^2", "heading_path^1.5"],
                       "type": "best_fields"}},
      # group 2: prose body (IK)
      {"match": {"chunk_text": {"query": q, "minimum_should_match": COVERAGE}}},
      # group 3: identifiers / code keywords (no stopword list)
      {"match": {"chunk_text.code": {"query": q, "boost": CODE_BOOST}}},
    ],
    "minimum_should_match": 1,
    "filter": [...tag...],
  }
}
```

Why this over the measured alternatives:

- `most_fields` (variant C) fixes the inversion but sums *all four* fields,
  so C1's title duplication is amplified with no way to exempt it.
- `best_fields` + `tie_breaker` (variant B) fixes the inversion weakly and
  still cannot express "max here, sum there".
- The explicit `bool.should` (variant D) is the only shape where grouping is
  a first-class decision. It measured within 1 rank of `most_fields` on the
  D1 probe while keeping title duplication controllable.

Boosts stay at today's values (`title^2`, `heading_path^1.5`,
`chunk_text.code^1.5`, `chunk_text` implicit 1.0) as the starting point;
G3 calibration may adjust `CODE_BOOST` and must record the final value here.

## Change 3 — coverage gate replaces the absolute floor (R4, R5)

Term coverage is scale-free: it asks "how much of the query did this
document actually match", which is comparable across queries in a way BM25
`_score` is not (D5).

Placement: `minimum_should_match` on the **`chunk_text` leaf only**. The
prose field is the one whose IK tokenization defines "the query's terms";
applying the same percentage to `chunk_text.code` would mean something
different because that analyzer keeps the English stopwords IK drops (D6),
and applying it to the title/heading group would penalize short breadcrumbs.
The outer `minimum_should_match: 1` keeps the bool a plain OR across groups.

New setting in the search relevance gates block:

| Field | Type | Default | Meaning | Disable |
|-------|------|---------|---------|---------|
| `SEARCH_BM25_MIN_COVERAGE` | `str` | `"70%"` (provisional) | `minimum_should_match` applied to the `chunk_text` leaf | `""` omits the key entirely |

`70%` is the starting point from the D6 measurement (it is the loosest value
that zeroes `股票基金定投策略`). It also cut `缓存穿透怎么解决` from 13 to 2
hits, so G3 must confirm the surviving 2 are the right ones before the value
is locked; if they are not, the fallback is `"2<70%"` (require all terms for
1–2 term queries, 70% beyond that) or a lower percentage plus acceptance
that the noise query returns 1 hit.

`SEARCH_BM25_MIN_SCORE` keeps its Settings field, `filter_es_hits`, and the
ES-side `min_score` plumbing, but its default becomes `0.0` (disabled) with
a comment recording *why* an absolute BM25 floor cannot work. Rationale for
keeping rather than deleting: the mechanism is eight tested lines and is a
legitimate operator escape hatch; deleting it would also churn
`SearchOutcome.es_gated` and the retriever constructor for no behavioral
gain. The risk of a future reader re-enabling it blindly is handled by the
comment plus D5's numbers in this document.

## Data flow / contracts

Unchanged: `Retriever.retrieve` → `bm25_chunk_query` → `search_chunks` →
`filter_es_hits` → `fuse_rrf`. `EsChunkHit`, `SearchHit`, and the API
response shape are untouched. `SearchOutcome.es_gated` keeps its meaning and
simply reports 0 once the absolute floor is disabled.

## Compatibility and migration

Analyzer changes do not apply in place (C2). Migration, matching the
`search-guidelines.md` runbook:

1. `curl -XDELETE localhost:9200/kb_documents`
2. reset `documents.index_status` from `done` to `pending`
3. `uv run python -m app.cli reindex --limit 100`

Re-indexing re-embeds every chunk (the vector column is rewritten with the
same `embedding_input`), so the vector leg is unchanged in shape but the
embedding calls are re-spent — trivial at 40 documents. README's migration
list gains an entry.

## Edge cases

| Case | Behavior |
|------|----------|
| Pure-CJK query | `code_search` leaves it as punctuation-delimited blobs that match nothing; groups 1+2 carry the query, as today |
| Pure-identifier query (`ConnectionPool`) | group 3 matches via `[connection, pool]`; group 2 contributes nothing (IK does not split it) |
| Query shorter than the coverage denominator | `70%` of a 1-term query rounds to 1 term — no behavior change for single-term queries |
| Coverage disabled (`""`) | `minimum_should_match` key omitted; hit counts return to the "none" column of the D6 table |
| Tag filter | still a `filter` clause on the same bool — unchanged, does not affect scoring |
| Vector-leg failure / BM25-only mode | untouched; the only `rag/retriever.py` change is the constructor pass-through of the coverage setting — fusion logic is never reached by this task |

## Rollback

Two independent levers, no schema migration:

- Query/gate changes: single revert of `search/queries.py` + config; no
  reindex needed.
- Analyzer changes: revert `search/es.py` and re-run the same three-step
  migration.

`SEARCH_BM25_MIN_COVERAGE=""` disables the new gate at runtime without a
deploy.

## Files touched (expected)

| File | Change |
|------|--------|
| `search/es.py` | `code_search` analyzer, `remove_duplicates` on `code`, `search_analyzer` binding |
| `search/queries.py` | `bool.should` group shape, coverage parameter, boost constants |
| `core/config.py` | `SEARCH_BM25_MIN_COVERAGE`; `SEARCH_BM25_MIN_SCORE` default → 0.0 + rationale |
| `api/deps.py` | inject the coverage setting |
| `rag/retriever.py` | constructor pass-through of the coverage setting into `bm25_chunk_query` — the one deviation from R9's literal file list, following the established gates pattern (directory-structure.md: gate thresholds are `Settings` → `_build_retriever` → `Retriever` constructor). No fusion/gate-logic change. |
| `.env.example` | new setting entry |
| `README.md` | migration runbook entry |
| `tests/test_es_queries.py` | new body shape, coverage sentinel, no-`analyzer`-key assertions |
| `tests/test_es_store.py` | `code` vs `code_search` token streams, duplicate-token assertion, mapping assertion |
| `tests/test_es_relevance.py` (new, `es`) | identifier regression, D1 rank ordering, C1 title-duplication, noise → 0, relevance probe set ≥ 1 |
| `tests/test_search_gates.py` | drift-guard extension |

## Calibration

Measured 2026-09-08 (G3) against the rebuilt live `kb_documents` index (16
documents, 40 chunk docs) through the shipped `bm25_chunk_query` (three-group
`bool.should`, coverage on the `chunk_text` leaf). "Before" columns are the
pre-overhaul measurements from this document's D1/D5/D6 sections (old shape:
single `multi_match` `best_fields`; old analyzer on `chunk_text.code`).

### Final values

| Setting | Final | Reasoning |
|---------|-------|-----------|
| `SEARCH_BM25_MIN_COVERAGE` | `"70%"` (provisional default kept) | Loosest value that zeroes the noise set; every relevance query keeps hits; the `缓存穿透怎么解决` survivors at 70% are the RIGHT ones (see below), so the `"2<70%"` fallback is unnecessary — it is strictly stricter (8→5 on `if not None 判断`, 4→3 on `Redis 一致性`) with zero noise benefit |
| `CODE_BOOST` | `1.5` (unchanged) | D1 ordering and the C1 synthetic test pass at this value; `setState 状态管理` ranks the both-halves chunks 1-2 with clear separation (39.99 / 30.99 vs 15.67) |

### Hit counts by coverage (after G1+G2, rebuilt index)

| query | none | 50% | **70%** | 100% | 2<70% | before (none, old shape) |
|-------|-----:|----:|----:|-----:|------:|-------------------------:|
| setState 状态管理 | 16 | 16 | **10** | 10 | 10 | 12 |
| MVCC 间隙锁 | 12 | 12 | **6** | 5 | 6 | 12 |
| 缓存穿透怎么解决 | 13 | 2 | **2** | 2 | 2 | 13 |
| for 循环怎么写 | 23 | 23 | **11** | 10 | 11 | 23 |
| if not None 判断 | 8 | 8 | **8** | 5 | 5 | 8 |
| Redis 一致性 | 4 | 4 | **4** | 3 | 3 | 4 |
| 股票基金定投策略 (noise) | 4 | 1 | **0** | 0 | 0 | 4 |
| 如何做红烧肉 (noise) | 0 | 0 | **0** | 0 | 0 | 0 |
| 今天天气怎么样 (noise) | 0 | 0 | **0** | 0 | 0 | 0 |

(`setState 状态管理` ungated grew 12 → 16: with the flat `code_search`
analyzer the identifier half matches by parts as a plain OR instead of an
unmatchable adjacency phrase — the D2 fix adding recall, as designed.)

### Rank positions (chosen coverage 70%)

- `setState 状态管理` — BEFORE: 状态管理 > BLoC ranked *3rd* (16.08) below
  Widget 体系 (19.70, rank 2). AFTER: 1. 状态管理 root (39.99), 2. 状态管理 >
  BLoC (30.99), 3. Widget 体系 (15.67). The both-halves chunks lead.
- `MVCC 间隙锁` — AFTER rank 1: MySQL 核心机制 > 锁：行锁、间隙锁与 next-key
  lock (25.22) — the gap-lock section itself.
- `缓存穿透怎么解决` — the 2 survivors at 70% are the right ones: 1. Redis
  缓存模式：穿透、击穿、雪崩与一致性 root (16.15), 2. same document >
  数据结构选型速查 (9.67). Correct document, correct chunk first.
- `Redis 一致性` — AFTER rank 1: Redis 缓存模式 root (22.43).
- Identifier regressions (D2): `ConnectionPool` → 2 hits, `async_bulk` → 12
  hits (both 0 before); `connection pool` keeps its 2 hits. The loose
  `async_bulk` count is expected: the code group carries no coverage gate
  (coverage is prose-leaf-only by design), so its OR over `[async, bulk]`
  reaches every chunk mentioning `async` — the RRF fusion side owns final
  ranking.

Noise → 0 and relevance ≥ 1 are pinned live by
`tests/test_es_relevance.py` (`test_noise_queries_return_no_hits`,
`test_relevance_probe_queries_keep_hits`); re-calibrate — do not delete —
if corpus growth shifts the counts.
