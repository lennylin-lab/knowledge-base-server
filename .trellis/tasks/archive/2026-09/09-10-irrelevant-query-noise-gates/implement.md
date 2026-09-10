# Implementation plan

Two independent holes. **Hole 2 (BM25) is a low-risk query-layer fix and can
land first. Hole 1 (vector rescue) is test-first: the live calibration probe is
a hard gate — no production default/wiring is finalized until the real-corpus
distances are measured and the trigger value is chosen.**

Layering / conventions: `uv run <cmd>` is canonical; rescue logic stays in
`rag/`, query-building in `search/`; docs in English; no `session.query()`.

## Stage A — Hole 2: BM25 identity-group coverage (low risk, no calibration)

- [x] A1. In `search/queries.py::bm25_chunk_query`, build the identity
      `multi_match` as a dict and attach `minimum_should_match = min_coverage`
      when `min_coverage` is truthy (mirror the prose leaf). Keep
      `chunk_text.code` uncovered. Update the docstring to state the identity
      group now honors coverage.
- [x] A2. `tests/test_es_queries.py`: assert the emitted body carries
      `minimum_should_match` on the identity `multi_match`; assert an empty
      `min_coverage` omits it (disable contract); document (via test intent)
      that a 4-token/one-stopword query cannot activate the group while a
      single-term query still can.
- [x] **Review gate A**: `uv run ruff check` + `uv run mypy` + `uv run pytest
      tests/test_es_queries.py` green. Confirm no other caller of
      `bm25_chunk_query` breaks. (Verified 2026-09-10 by check agent:
      callers are `rag/retriever.py` and the live-ES relevance tests only.)

## Stage B — Hole 1 CALIBRATION (test-first, HARD GATE) 🔴

Do NOT proceed to Stage C until B is complete and the trigger value is recorded
in `design.md`.

- [x] B1. Extend `tests/test_vector_distance_probe.py` (or add a `live_llm`
      sibling) to embed, through the real endpoint and using the real-corpus
      `embedding_input` wrapping: genuine short in-domain keywords
      (`缓存`, `锁`, `事务`, `分布式`, `索引`, …) and the five out-of-domain
      queries from `prd.md`. Print per-query min/max cosine distance against the
      real 15-doc corpus chunk inputs (or a faithful representative set).
- [x] B2. Run manually: `uv run pytest -m live_llm
      tests/test_vector_distance_probe.py -s`. Record the min/max table in
      `design.md` (Calibration section).
- [x] B3. Decide the trigger:
      - clean separation → pick a value between the in-domain and out-of-domain
        `leg_min` bands; record as the new Settings default.
      - overlap → record the precision/recall tradeoff and make an explicit
        policy decision with the user; record the chosen value + rationale.
- [x] **Review gate B (STOP)**: trigger value + measured evidence written to
      `design.md`; PRD AC2 satisfied. Get user sign-off on the value (and on the
      tradeoff, if bands overlap) before wiring. (Sign-off recorded in
      design.md § Calibration: "user-approved at gate B, 2026-09-10".)

## Stage C — Hole 1: on-domain trigger implementation

- [x] C1. `rag/retriever.py`: add
      `DEFAULT_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE` (= value from B3).
- [x] C2. `filter_vector_rows_with_rescue`: add
      `rescue_trigger_max_distance` kwarg; after the primary tier empties and
      `measured` is computed, return `([], 0)` when
      `min(measured) > rescue_trigger_max_distance`; keep existing disable
      short-circuits and the `>= 2.0` disable sentinel for the trigger. Update
      the docstring.
- [x] C3. `Retriever.__init__`: add
      `vector_rescue_trigger_max_distance` kwarg (default = new constant); pass
      it into the helper call in `retrieve`.
- [x] C4. `core/config.py`: add
      `SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE: float = <B3 value>` with a
      docstring sentinel (`>= 2.0` disables).
- [x] C5. `api/deps.py::_build_retriever`: inject the setting.
- [x] C6. `.env.example`: add the new setting with a comment.

## Stage D — Tests & regression

- [x] D1. `tests/test_search_gates.py`: unit cases for the on-domain trigger —
      off-domain leg (`leg_min` above trigger) rescues nothing; in-domain leg
      (`leg_min` between 0.45 and trigger) still rescued; trigger `>= 2.0`
      reproduces pre-task behavior; extend the config↔`DEFAULT_*` drift-guard.
- [x] D2. `tests/test_retriever.py`: integration — out-of-domain vector-only leg
      → 0 items; in-domain short-keyword leg → rescued and fused.
- [x] D3. `tests/test_search_api.py`: out-of-domain query → 0 items smoke, if a
      representative fixture corpus is available (otherwise rely on D1/D2 +
      manual repro).
- [x] **Review gate D**: full offline suite + `ruff` + `mypy` green:
      `uv run ruff check && uv run mypy && uv run pytest` (default markers,
      `live_llm` excluded). (Verified 2026-09-10 by check agent: 421 passed,
      only the two known pre-existing corpus-pinned `test_es_relevance`
      failures.)

## Stage E — Manual end-to-end verification (against real corpus)

- [x] E1. With the 15-doc corpus indexed, run the five out-of-domain queries via
      `GET /api/v1/search?q=...&limit=50` → each returns 0 items (AC1).
- [x] E2. Confirm "量子力学薛定谔的猫" BM25 leg = 0 (AC5) and relevant queries
      ("Redis 分布式锁", "MySQL 事务隔离") still return their expected hits (AC3/R3).
      (Verified 2026-09-10 against the live stack: all five noise queries → 0
      items; quantum query BM25 leg = 0 on dev ES; "Redis 分布式锁" → 5 items
      with the Redis document ranked first, "MySQL 事务隔离" → 7 items.)

## Stage F — Leftover: re-anchor 09-08 D1/D2 live regressions (discovered at gate D)

- [x] F1. `tests/test_es_relevance.py`: the dev `kb_documents` corpus was
      replaced after 09-08, stranding the D1/D2 anchors (ConnectionPool,
      async_bulk, BLoC/Widget). Re-anchored per the file's own
      re-calibration protocol: `useState`/`lru_cache` (one anchor per
      naming convention), plain-words `use state`/`lru cache` presence
      (totals no longer pinned — corpus-mutable), D1 both-halves
      React 渲染与并发 chunk vs React title-only chunk (measured 18.84
      rank 1 vs 16.03 rank 4). Spec §5/§6 notes updated.
      (Verified 2026-09-10: full suite 423 passed / 0 failed / 7 deselected.)

## Validation commands

```bash
uv run ruff check
uv run mypy
uv run pytest                                   # offline suite (live_llm excluded)
uv run pytest -m live_llm tests/test_vector_distance_probe.py -s   # B2 calibration only
```

## Rollback points

- Hole 2: revert the `search/queries.py` change, or set
  `SEARCH_BM25_MIN_COVERAGE=""` (disables all coverage, including identity).
- Hole 1: set `SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE=2.0` (trigger off →
  pre-task rescue behavior) without a code revert; or revert the commit.
- No DB migration, no reindex — single revert restores prior behavior.

## Ordering / dependency notes

- Stage A is independent and may land first.
- Stage C depends on Stage B's recorded trigger value — do not hardcode a
  guessed default. B is the test-first gate the task title requires.
