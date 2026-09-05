# Implement: Short-query vector gate calibration (head-rescue)

Ordered checklist. Gates after each group; single commit at end (no schema
change). **Do not run `task.py start` until the user approves the planning
summary.**

## G1 — Settings + pure helpers (offline first)

- [x] `core/config.py`: add `SEARCH_VECTOR_RESCUE_MARGIN` (0.15, `<= 0`
      disables), `SEARCH_VECTOR_RESCUE_MAX_DISTANCE` (0.85, `<= 0`
      disables), `SEARCH_MAX_QUERY_LENGTH` (256, `<= 0` disables) with
      documented sentinels
- [x] `rag/retriever.py`: `filter_vector_rows_with_rescue` pure helper
      (primary tier → on-empty rescue window `min(leg_min + margin, cap)`;
      returns kept rows + rescued count); keep `filter_vector_rows` for
      tests; `Retriever.__init__` gains `vector_rescue_margin` /
      `vector_rescue_max_distance` / `max_query_length` kwargs mirroring
      Settings defaults; truncate the query once at `retrieve()` entry
- [x] `tests/test_search_gates.py`: rescue unit cases — disabled sentinels,
      primary survivors → no rescue, rescue admits head within window,
      cap rejects high `leg_min`, empty rows, `None`-distance fail-open;
      truncation unit cases — over-long truncated, exact-bound untouched,
      disabled sentinel no-op; extend the Settings↔Retriever drift-guard test
- [x] Validation: `uv run pytest tests/test_search_gates.py`; ruff + mypy

## G2 — Retriever wiring + test world + live probe

- [x] `rag/retriever.py`: switch the vector gate to the rescue helper;
      `SearchOutcome.vector_rescued` counter
- [x] `tests/corpus.py` / `tests/fakes.py`: `shifted_scripted_provider`
      (relevant head at distance 0.50–0.65 — above 0.45, inside rescue
      window; unrelated tail ≥ 0.9)
- [x] `tests/test_retriever.py`: shifted-world short query → hybrid items
      with `vector_rescued > 0`; unrelated query → still 0 items;
      "zorblat"-style head at ≥ 0.9 → no rescue, ES-dominated;
      over-long query → legs receive the truncated prefix; GATES_OFF
      fixture unchanged
- [x] `tests/test_vector_distance_probe.py` (new, `@pytest.mark.live_llm`):
      embed fixed short keywords + chunk-like texts via the real endpoint,
      print distance matrix — no DB/ES, excluded from default run
- [x] Run the probe once against the dev endpoint; record head-distance
      numbers + final margin/cap in design.md Calibration section
- [x] Validation: db+es marked tests with compose up; `uv run pytest
      -m live_llm tests/test_vector_distance_probe.py` manually

## G3 — Observability + deps wiring + full gates

- [x] `services/search.py`: `vector_rescued` into `search_executed`
      (count only, never query text)
- [x] `api/deps.py`: inject the two new thresholds into Retriever
- [x] `.env.example`: new settings entries
- [x] `tests/test_search_api.py`: assert `vector_rescued` appears in
      `search_executed` logs on a rescue path; over-long-q smoke — 200 with
      results, no 502 (`SearchHit` schema unchanged)
- [x] Validation: full `uv run pytest`; `uv run ruff check src tests`;
      `uv run ruff format --check src tests`; `uv run mypy src`

## Review gates

- trellis-check after G3: spec compliance, PRD acceptance sweep, empty-over-
  noise + degradation paths unchanged, rescue logic confined to `rag/`,
  gates re-run.

## Rollback

- Single revert; `SEARCH_VECTOR_RESCUE_MARGIN=0` hotfix restores pre-task
  behavior without deploy.
