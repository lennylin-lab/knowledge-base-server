# Implement: ES BM25 scoring overhaul

Ordered checklist. The groups are strictly sequential: G1 changes what tokens
a query produces, so calibrating G2/G3 before G1 lands would be wasted work.
Gates after each group; single commit at the end (no schema change).

**Do not run `task.py start` until the user approves the planning summary.**

## G1 — Analyzer split + reindex (R1, R2, R7)

- [x] `search/es.py`: add `code_delimiter_search` filter
      (`preserve_original=false`, `catenate_words=false`,
      `split_on_case_change=true`, `split_on_numerics=false`,
      `stem_english_possessive=false`) and the `code_search` analyzer
      (whitespace + that filter + `lowercase`, no `flatten_graph`)
- [x] `search/es.py`: append `remove_duplicates` to the `code` analyzer's
      filter chain, after `lowercase`
- [x] `search/es.py`: bind `"search_analyzer": "code_search"` on the
      `chunk_text.code` subfield in `_CHUNK_MAPPINGS`
- [x] `tests/test_es_store.py`: extend the mapping assertion; assert
      `code` emits `setstate` exactly once for `setState`; assert
      `code_search` emits a flat `[connection, pool]` for `ConnectionPool`
      (one token per position, no `positionLength > 1`)
- [x] Run the migration: drop `kb_documents`, reset `documents.index_status`
      `done → pending`, `uv run python -m app.cli reindex --limit 100`;
      confirm all 40 documents return to `done`
- [x] `README.md`: add this task to the migration runbook list
- [x] Validation: `uv run pytest -m es tests/test_es_store.py`; ruff + mypy

**Rollback point**: revert `search/es.py`, re-run the same migration.

## G2 — Query shape (R3)

- [x] `search/queries.py`: replace the single `multi_match` with the
      three-group `bool.should` from design.md — identity group
      (`title^2`, `heading_path^1.5`, `best_fields` max), prose group
      (`chunk_text`), code group (`chunk_text.code^CODE_BOOST`);
      `minimum_should_match: 1` on the outer bool; tag filter unchanged
- [x] Keep the body analyzer-free (C3) — no `analyzer` key anywhere
- [x] `tests/test_es_queries.py`: pin the new body shape; assert the tag
      filter still does not perturb the `should` clauses; assert no
      `analyzer` key appears in the body
- [x] `tests/test_es_relevance.py` (new, `pytestmark = pytest.mark.es`):
      D1 ordering — `setState 状态管理` ranks `状态管理 > BLoC` above
      `Widget 体系`; C1 — a title-only match does not beat an equally-strong
      body+code match; D2 regression — `ConnectionPool` and `async_bulk`
      each return ≥ 1 hit while `connection pool` keeps its 2
- [x] Validation: `uv run pytest -m es`; ruff + mypy

**Rollback point**: revert `search/queries.py` alone; no reindex needed.

## G3 — Coverage gate + calibration (R4, R5, R6, R8)

- [x] `core/config.py`: add `SEARCH_BM25_MIN_COVERAGE` (default `"70%"`,
      `""` disables) in the search relevance gates block; change
      `SEARCH_BM25_MIN_SCORE` default to `0.0` with a comment recording why
      an absolute BM25 floor cannot work (cite D5's measured score spread)
- [x] `search/queries.py`: thread the coverage value onto the `chunk_text`
      leaf only; omit the key entirely when the value is empty
- [x] `api/deps.py`: inject the coverage setting; `.env.example` entry
- [x] `tests/test_es_queries.py`: coverage present / omitted-on-sentinel
      offline cases
- [x] `tests/test_search_gates.py`: extend the Settings↔Retriever
      drift-guard for the new field
- [x] **Calibrate**: run the relevance probe set (`setState 状态管理`,
      `MVCC 间隙锁`, `缓存穿透怎么解决`, `for 循环怎么写`, `if not None 判断`,
      `Redis 一致性`) and the noise set (`股票基金定投策略`, `如何做红烧肉`,
      `今天天气怎么样`) against the rebuilt index. Confirm noise → 0 and
      every relevance query ≥ 1. Inspect *which* hits survive for
      `缓存穿透怎么解决` (13 → 2 at 70% pre-G1); if the survivors are wrong,
      fall back to `"2<70%"` per design.md
- [x] Record before/after counts, rank positions, and the final
      `SEARCH_BM25_MIN_COVERAGE` / `CODE_BOOST` in design.md § Calibration
- [x] `tests/test_es_relevance.py`: noise → 0 hits; every relevance-probe
      query ≥ 1 hit
- [x] Validation: full `uv run pytest` (compose up for db+es);
      `uv run ruff check src tests`; `uv run ruff format --check src tests`;
      `uv run mypy src`

**Rollback point**: `SEARCH_BM25_MIN_COVERAGE=""` disables the gate without
a deploy.

## Review gate

- `trellis-check` after G3: PRD acceptance sweep, spec compliance
  (`search-guidelines.md` C3 — no analyzer keys in query bodies), scope
  discipline (R9 — nothing touched under `rag/` or `repositories/`),
  degradation paths unchanged, gates re-run.
- Spec update (Phase 3.3): `search-guidelines.md` needs the index-vs-search
  analyzer rule, the D2 phrase-query gotcha, and the "absolute BM25 floors do
  not generalize" finding. The existing claim that `connection pool` matches
  `ConnectionPool` must be corrected to state both directions.

## Risky files

- `search/es.py` — mapping changes are irreversible in place; every edit
  costs a reindex.
- `core/config.py` — the drift-guard test in `tests/test_search_gates.py`
  fails loudly on Settings↔Retriever mismatch; update both together.
