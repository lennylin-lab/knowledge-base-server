# Implement: Search relevance quality gates

Ordered checklist. Gates after each group; single commit at end (no schema
change). **Do not run `task.py start` until the user approves planning.**

## G1 — Settings + pure gate helpers (offline first)

- [x] `core/config.py`: add `SEARCH_BM25_MIN_SCORE`,
      `SEARCH_VECTOR_MAX_DISTANCE`, `SEARCH_RRF_MIN_RELATIVE` with documented
      disable sentinels
- [x] `rag/retriever.py`: `apply_relative_score_floor` pure function;
      threshold filtering helpers for ES hits and vector rows
- [x] `tests/test_search_gates.py` (new): relative floor unit cases — empty,
      disabled (`0.0`), single hit, trailing weak hits dropped, all-below-top
- [x] Validation: `uv run pytest tests/test_search_gates.py`; ruff + mypy

## G2 — Leg instrumentation + repository distance

- [x] `search/queries.py`: accept optional `min_score`, include in ES body when
      `> 0`
- [x] `repositories/document_chunk.py`: `search_similar` returns cosine
      distance per row (extend row type cleanly)
- [x] `rag/retriever.py`: apply leg gates; thread leg scores into
      `RetrievedChunk`; wire Settings thresholds via constructor
- [x] `tests/test_retriever.py`: add cases — unrelated vector query → 0 items
      with gates on; distinctive BM25 still hits; gates-off fixture preserves
      prior behavior
- [x] Validation: db+es marked tests with compose up

## G3 — API surface + deps wiring

- [x] `schemas/search.py`: `es_score`, `vector_distance` on `SearchHit`
- [x] `services/search.py`: map new fields; extend `search_executed` with
      gate count stats (no query text)
- [x] `api/deps.py`: pass Settings thresholds into Retriever construction
- [x] `tests/test_search_api.py`: assert new fields present on hybrid hit;
      unrelated query returns empty `items` when gates enabled
- [x] Update agent source mapping if `RetrievedChunk` fields surface in chat
      SSE (only if existing mapper drops unknown fields — verify)
- [x] Validation: full `uv run pytest`; ruff format --check; mypy src

## Review gates

- trellis-check after G3: spec compliance, PRD acceptance sweep, layering
  unchanged (gates in `rag/` only), gates re-run.

## Rollback

- Single revert; set disable sentinels in env as hotfix alternative.

## Calibration note

During G2, tune default Settings values against `tests/corpus.py` seeded
world so unrelated `VECTOR_QUERY` returns 0 gated hits while `zorblat` /
scripted neighbors still pass. Record final defaults in PR if they differ
from `design.md` starting points.
