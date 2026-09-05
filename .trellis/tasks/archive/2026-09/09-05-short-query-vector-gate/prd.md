# Short-query vector gate calibration

## Goal

Stop the fixed vector-distance ceiling from disproportionately silencing the
vector leg for short keyword queries. Today a short query's embedding sits
systematically farther from long chunks than a long natural-language query's
(granularity asymmetry), so the absolute ceiling `SEARCH_VECTOR_MAX_DISTANCE`
(0.45) can gate out the entire vector leg — fusion degenerates to pure BM25
and ES becomes the sole ranking decider exactly when semantic recall is
needed (vocabulary-mismatch short queries). Empty-over-noise semantics from
the 09-05-search-relevance-gates task must be preserved.

## Policy decision (user, 2026-09-05)

**Recall-first head rescue.** When the vector head is marginal-but-clustered
for a query, admit it to fusion; keep a hard noise floor so genuinely
unrelated legs stay silent (rare-term keywords remain ES-dominated). Chosen
over "tune the static threshold only" (still kills the leg on
vocabulary-mismatch queries) and over "confidence-weighted RRF" (fusion-level
change, makes ES stronger alone, deferred).

## Background (confirmed from codebase, 2026-09-05)

- RRF fusion is rank-only (`rag/retriever.py:76-106`, k=60): absolute
  `es_score`/`vector_distance` never influence fusion. The vector leg's only
  influence channels are its ranks and gate survival.
- Vector gate is a single fixed absolute ceiling
  (`filter_vector_rows`, `rag/retriever.py:119-128`; default 0.45,
  `core/config.py:75`). It cannot distinguish "whole leg shifted up by
  short-query granularity mismatch" from "leg is genuinely unrelated noise".
- BM25 gate (`SEARCH_BM25_MIN_SCORE` 1.0, `core/config.py:72`) and the
  post-fusion relative floor (`SEARCH_RRF_MIN_RELATIVE` 0.35,
  `apply_relative_score_floor` `rag/retriever.py:131-145`) are unchanged by
  this task unless measurement justifies a shift.
- Rare-term short keywords ("zorblat", IDs, code identifiers) SHOULD stay
  ES-dominated — vector silence there is correct behavior, not a bug.
- Dev environment has a real embedding endpoint (`.env`: Qwen3-Embedding-4B,
  1536-dim), so a live measurement of short-keyword distance distributions
  is feasible as a calibration step (manual `live_llm`-marked probe,
  excluded from the default test run per quality-guidelines).
- Test corpus has scripted provider infrastructure
  (`tests/corpus.py`: `neighbor_scripted_provider`, `distant_scripted_provider`)
  ready to extend with a "shifted-up but ordered" short-query world.

## Requirements

1. **Two-tier vector gate (head-rescue)** in `rag/retriever.py`: primary
   tier unchanged (`distance <= SEARCH_VECTOR_MAX_DISTANCE`); when the
   primary tier empties the leg, a rescue tier admits rows within
   `min(leg_min + SEARCH_VECTOR_RESCUE_MARGIN, SEARCH_VECTOR_RESCUE_MAX_DISTANCE)`
   (new Settings: margin 0.15, cap 0.85; `<= 0` on either disables rescue).
   Rescue never runs when the primary tier has survivors — behavior is
   bit-identical to today in that case.
2. **Measure before finalizing**: a `live_llm`-marked probe embeds fixed
   short keywords + chunk-like texts through the real endpoint and prints
   the distance matrix; run once during the task to confirm the mechanism
   and calibrate margin/cap; evidence recorded in design.md Calibration.
3. **Noise floor preserved**: the hard cap bounds every rescue; the
   post-fusion relative floor and empty-over-noise semantics are unchanged.
4. **Rare-term keywords stay ES-dominated**: a leg whose minimum distance
   exceeds the rescue cap rescues nothing.
5. **Settings-driven, single global set** (per-caller profiles remain out of
   scope); constructor-injected via `api/deps.py` with the existing
   drift-guard test extended.
6. **Shared retriever behavior**: change lives in `rag/retriever.py` only;
   search, QAAgent, WritingAgent all benefit; no gate logic in agents or
   routers.
7. **Observability**: `SearchOutcome`/`search_executed` gain a
   `vector_rescued` count (rows admitted only via rescue; count only, no
   query text); `SearchHit` schema unchanged.
8. **Degradation preserved**: BM25-only and vector-leg-failure paths
   untouched; rescue operates on the tag-filtered leg result the same way
   the current gate does.
9. **Query length cap**: neither search path bounds query length today
   (API `q` has `min_length=1` only, `api/v1/endpoints/search.py:18`; agent
   tools take bare `str`, `agents/qa.py:131`, `agents/writing.py:71`). A
   single enforcement point in `Retriever.retrieve()` truncates over-long
   queries to `SEARCH_MAX_QUERY_LENGTH` (new Settings, default 256 chars,
   `<= 0` disables). Rationale: standard-analyzer CJK text yields ~1 token
   per char (`search/es.py:24-31` sets no analyzer), and Lucene's
   `max_clause_count` of 1024 turns a multi-thousand-char query into a
   `query_shard_exception` → `SearchIndexError` 502; 256 << 1024 eliminates
   the failure by construction, and the cap also keeps embedding inputs
   under provider token limits (over-limit embed currently degrades the
   vector leg silently, `rag/retriever.py:319-322`). One mechanism serves
   API and agent paths; agents cannot receive a 422.
10. **Tests**: offline unit tests for the rescue helper and the truncation
   bound; retriever integration tests with a `shifted_scripted_provider`
   (relevant head 0.50–0.65, unrelated tail ≥ 0.9) proving short-query
   recall restored while unrelated-query empty-over-noise and GATES_OFF
   legacy behavior still hold; API-level test proving an over-long query
   returns results (no 502).

## Acceptance Criteria

- [ ] Probe evidence recorded: head-distance distribution for short vs long
      queries on the real endpoint; final margin/cap derived and noted in
      design.md Calibration (defaults 0.15 / 0.85 unless evidence shifts).
- [ ] Shifted-world short query (head 0.50–0.65) returns hybrid results with
      `vector_rescued > 0` — recall restored (integration test).
- [ ] Unrelated query still returns zero items despite `limit`; existing
      gate tests stay green (no empty-over-noise regression).
- [ ] Rare-term short keyword (head ≥ 0.9) rescues nothing — ES-dominated
      behavior unchanged (integration test).
- [ ] Rescue disabled via sentinels (`margin <= 0` or `cap <= 0`) reproduces
      today's single-tier gate exactly (unit test).
- [ ] `search_executed` logs `vector_rescued` on the rescue path; `SearchHit`
      API shape unchanged.
- [ ] Over-long query (well past 256 chars, e.g. 3000 CJK chars) returns
      results via the truncated prefix — no 502, no exception (API test).
- [ ] Query truncation disabled via `SEARCH_MAX_QUERY_LENGTH <= 0`
      (sentinel, unit-tested).
- [ ] Gates green: `uv run pytest` (db+es where marked), `ruff check src tests`,
      `ruff format --check src tests`, `mypy src`.

## Out of Scope

- Confidence-weighted RRF (leg-weight fusion change) — follow-up candidate.
- Query expansion / LLM query rewriting.
- Per-caller strictness profiles; query-time API parameters.
- Re-calibrating RRF `k`, `CANDIDATE_POOL`, BM25 floor, or relative floor
  (unless probe evidence explicitly justifies it).
- Changing `SEARCH_VECTOR_MAX_DISTANCE` itself (primary tier stays 0.45).
- Chinese-aware ES analyzers (IK etc.) or any index mapping/analysis change —
  separate relevance-engineering task; the length cap removes the failure
  mode without touching the index.
