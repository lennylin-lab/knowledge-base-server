# ES BM25 scoring overhaul: cross-field evidence and gate calibration

## Goal

Make the BM25 leg score *correctly* on a mixed Chinese-prose + source-code
corpus. Tasks `09-06-es-ik-analyzer` and `09-06-code-aware-chunking`
installed the right *fields* (IK for CJK, a `code` subfield for identifiers,
`heading_path` breadcrumbs) but left *scoring* on `multi_match` `best_fields`
defaults. Measurement against the live index shows the leg cannot combine
evidence across those fields, silently degrades identifier queries into
phrase queries, double-counts camelCase term frequency, and runs behind an
absolute score floor that never fires.

This is the thorough (not minimal-patch) treatment of the ES side.

**User value**: searching `ConnectionPool` finds the connection-pool chunk;
searching `setState 状态管理` ranks the chunk that matches *both* halves
first; searching something the knowledge base has nothing about returns
nothing from the BM25 leg.

## Background: confirmed facts (measured 2026-09-08)

Environment: live ES 8.17.3 (`kb_documents`, 40 chunk docs, Chinese
programming documentation). The live index **already carries** the current
`_CHUNK_MAPPINGS` + `_CHUNK_SETTINGS`, so the `search-guidelines.md`
migration was completed and every measurement below reflects production
shape.

Current query shape (`search/queries.py:22-27,46`):

```python
_MATCH_FIELDS = ["chunk_text", "chunk_text.code^1.5", "heading_path^1.5", "title^2"]
{"multi_match": {"query": q, "fields": _MATCH_FIELDS}}   # best_fields, tie_breaker=0
```

### D1 — `best_fields` cannot combine cross-field evidence (rank inversion)

`best_fields` takes the single best field score per document; with the
default `tie_breaker=0` a document matching the Chinese topic **and** the
code identifier scores no higher than one matching only the strongest single
field. Measured, query `setState 状态管理`:

| variant | rank 2 | rank 3 |
|---------|--------|--------|
| A. current (`best_fields`, tie_breaker 0) | Widget 体系与三棵树渲染 (19.70) | 状态管理 > BLoC (16.08) |
| B. `best_fields` + tie_breaker 0.3 | 状态管理 > BLoC (26.35) | Widget 体系 (21.57) |
| C. `most_fields` | 状态管理 > BLoC (50.30) | Widget 体系 (25.94) |
| D. `bool.should` (IK group + code group) | 状态管理 > BLoC (30.62) | Widget 体系 (25.94) |

The chunk matching both the query's Chinese half and its identifier half
ranks *below* an off-topic chunk under the current shape, and above it under
every additive variant. Primary defect.

### D2 — identifier queries silently become phrase queries (0 hits)

`chunk_text.code` declares one analyzer for both index and search
(`search/es.py:50-55`), so a query is expanded by `word_delimiter_graph` with
`preserve_original` + `catenate_words` into a *stacked token graph*. Lucene
compiles the stacked alternative into an adjacency-constrained phrase:

```
_validate/query  match chunk_text.code: "ConnectionPool"
  -> (chunk_text.code:connectionpool chunk_text.code:connectionpool
      chunk_text.code:"connection pool")        <-- PHRASE, requires adjacency
_validate/query  match chunk_text.code: "connection pool"
  -> chunk_text.code:connection chunk_text.code:pool        <-- plain OR
```

Measured end to end: `ConnectionPool` → **0 hits**, `async_bulk` → **0 hits**,
`connection pool` → 2 hits. The corpus holds `connection` (1 doc) and `pool`
(1 doc) but not adjacent. The subfield's headline promise in
`search-guidelines.md` ("`connection pool` has to match `ConnectionPool`")
holds only in that one direction.

### D3 — camelCase identifiers are indexed twice (BM25 tf inflation)

`preserve_original` and `catenate_words` emit the *same string* for a pure
camelCase identifier, so the index stores a duplicate token at one position:

```
code("ConnectionPool") -> connectionpool(p0) connectionpool(p0) connection(p0) pool(p1)
code("setState")       -> setstate(p0)       setstate(p0)       set(p0)        state(p1)
code("async_bulk")     -> async_bulk(p0)     asyncbulk(p0)      async(p0)      bulk(p1)   # distinct, OK
```

Every camelCase identifier carries `tf=2` instead of `1`, inflating its BM25
contribution relative to snake_case and prose terms.

### D4 — `SEARCH_BM25_MIN_SCORE = 1.0` never fires

Across 11 probe queries every returned hit scored ≥ 1.0 (lowest score
observed anywhere: 1.035). The Python gate `filter_es_hits`
(`rag/retriever.py:116-123`) and the ES-side `min_score`
(`search/queries.py:54-55`) drop nothing in practice.

### D5 — BM25 score scale is query-dependent, so no absolute floor can work

Top-hit score by query: `for 循环怎么写` 4.46, `缓存` 4.86, `MVCC 间隙锁`
12.03, `setState 状态管理` 28.39. The min/top ratio within one result set
ranges 0.045–0.586. An absolute threshold calibrated for one query is
meaningless for another — the same defect class as the vector leg's fixed
distance ceiling.

### D6 — the ES leg feeds noise into fusion for unrelated queries

Query `股票基金定投策略` (nothing in the corpus is about finance) returns 4
hits scoring 2.07–4.49, all passing the 1.0 floor, all entering RRF with real
ranks. (`今天天气怎么样` correctly returns 0 — IK word segmentation makes ES
*sometimes* silent, but not reliably.)

A **relative** score floor cannot fix this: that noise set's own min/top
ratio is 0.46, so it survives any floor loose enough to keep real results.
The scale-free lever is *term coverage*, not score. Measured hit counts for
`minimum_should_match` on the current field set:

| query | none | 50% | 70% | 100% |
|-------|-----:|----:|----:|-----:|
| 股票基金定投策略 (noise) | 4 | 1 | **0** | 0 |
| 如何做红烧肉 (noise) | 0 | 0 | 0 | 0 |
| setState 状态管理 | 12 | 12 | 6 | 3 |
| MVCC 间隙锁 | 12 | 12 | 4 | 2 |
| 缓存穿透怎么解决 | 13 | 2 | 2 | 0 |
| for 循环怎么写 | 23 | 23 | 11 | **0** |
| if not None 判断 | 8 | 5 | 5 | **0** |
| Redis 一致性 | 4 | 4 | 4 | 2 |

`100%` destroys legitimate queries; `70%` silences the noise query but also
cuts `缓存穿透怎么解决` from 13 to 2. Calibration is real work, and it
interacts with IK's English stopword list: `for 循环怎么写` analyzes to
`[循环, 怎么, 写]` and `if not None 判断` to `[none, 判断]` on the IK fields
while the `code` subfield keeps the dropped keywords, so one
`minimum_should_match` percentage means different things per field.

### C1 — constraint: `heading_path` contains `title`

`Chunk.heading_path` is the markdown ancestor breadcrumb
(`rag/chunker.py:19-30`) and each document's H1 is its title, so indexed
`heading_path` values start with the title text (chunk 0's breadcrumb equals
the title exactly; deeper chunks are `"<title> > <section>"`). Under
`best_fields` (max) this is harmless. **Any additive scoring scheme counts a
title match two or three times** — `title^2` + `heading_path^1.5` + possibly
`chunk_text`. The D1 fix must handle this or it amplifies title matches ~3.5x.

### C2 — constraint: analyzer changes require a full reindex

Per `search-guidelines.md`, ES validates mappings but never re-analyzes in
place. Any D2/D3 fix means: drop index → reset `documents.index_status` to
`'pending'` → `python -m app.cli reindex`. The CLI deliberately offers no
full-rebuild flag. Cost at 40 chunks is trivial; the procedure has already
been executed twice this month.

### C3 — constraint: mapping is the only analyzer declaration site

`search-guidelines.md` forbids an `analyzer` key in query bodies. A separate
search-side analyzer bound via `search_analyzer` in `_CHUNK_MAPPINGS` is
compliant; overriding the analyzer per query is not.

## Requirements

1. **R1 — Split the `code` analyzer into index and search variants.** The
   search-side analyzer must emit a flat, unstacked token stream so Lucene
   never generates an adjacency phrase from an identifier query. Declared in
   `_CHUNK_SETTINGS` and bound via `search_analyzer` on the `chunk_text.code`
   subfield (C3).
2. **R2 — De-duplicate index-time tokens** so a camelCase identifier is
   stored once per position (D3).
3. **R3 — Replace `best_fields` with an additive cross-field shape** that
   sums evidence across genuinely independent field groups while taking the
   max *within* the overlapping `title` / `heading_path` group, so a title
   match is never counted twice (D1 + C1).
4. **R4 — Replace the absolute BM25 floor with a term-coverage criterion.**
   Coverage is expressed as `minimum_should_match`, settings-driven, with a
   documented disable sentinel, and calibrated against a recorded probe set
   (D4 + D5 + D6).
5. **R5 — Retire `SEARCH_BM25_MIN_SCORE` as a live gate**, with the reason
   documented at the settings site so it is not silently re-enabled.
6. **R6 — Preserve empty-over-noise.** An unrelated query must yield zero ES
   hits, and the existing degradation paths (vector-leg failure, BM25-only
   mode, tag filtering) must be unchanged.
7. **R7 — Execute and document the reindex migration** required by R1/R2,
   extending the README runbook migration list (C2).
8. **R8 — Calibration evidence recorded** in `design.md`: per-query hit
   counts and rank positions before and after, on the real corpus, for both
   the relevance probe set and the noise probe set.
9. **R9 — Scope discipline**: all changes confined to `search/es.py`,
   `search/queries.py`, `core/config.py`, `api/deps.py`, and tests. No
   chunker, embedding, retriever-fusion, or PG changes.

## Acceptance Criteria

- [ ] `ConnectionPool` and `async_bulk` each return ≥ 1 hit (live-ES
      regression test); `connection pool` still returns its current 2 hits.
- [ ] `_validate/query` for an identifier query against `chunk_text.code`
      contains no phrase clause (live-ES test asserting the explanation
      shape).
- [ ] `_analyze` on the index-time `code` analyzer emits `setstate` exactly
      once for input `setState` (live-ES test).
- [ ] Query `setState 状态管理`: the `状态管理 > BLoC` chunk ranks above the
      `Widget 体系` chunk (live-ES ranking test pinning the D1 inversion).
- [ ] A title-only match does not outscore an equally-strong body+code match
      purely through `title` / `heading_path` duplication (live-ES test
      derived from C1).
- [ ] Query `股票基金定投策略` returns 0 ES hits; `如何做红烧肉` and
      `今天天气怎么样` stay at 0.
- [ ] Every query in the relevance probe set (`setState 状态管理`,
      `MVCC 间隙锁`, `缓存穿透怎么解决`, `for 循环怎么写`, `if not None 判断`,
      `Redis 一致性`) returns ≥ 1 hit — the coverage gate must not silence
      legitimate queries.
- [ ] Coverage disabled via its sentinel reproduces pre-gate hit counts
      (offline unit test).
- [ ] `bm25_chunk_query` body shape is pinned by offline unit tests; query
      bodies contain no `analyzer` key (C3).
- [ ] `SEARCH_BM25_MIN_SCORE` no longer gates: the retired-gate rationale is
      present at the settings site, and the Settings↔Retriever drift-guard
      test covers the new coverage setting.
- [ ] README runbook lists the reindex migration for this task; the live
      index is rebuilt and all 40 documents return to `done`.
- [ ] Gates green: `uv run pytest` (db+es marked, compose up),
      `uv run ruff check src tests`, `uv run ruff format --check src tests`,
      `uv run mypy src`.

## Out of Scope

- Vector-leg rescue tier, `RRF_K`, the post-fusion relative floor, and
  query-side embedding changes — a separate follow-up task owns the fusion
  side. This task improves the BM25 leg and stops it feeding noise, but does
  not by itself close the end-to-end "unrelated query returns all documents"
  behavior.
- Adding a reranker / cross-encoder stage.
- Chunking changes (`rag/chunker.py`) and the embedding input shape
  (`rag/indexer.py::embedding_input`).
- Replacing IK with another CJK analyzer, or adding a custom IK dictionary.
- Removing the `code` subfield for pure-Chinese chunk text (index-size
  optimization only; no correctness impact).
