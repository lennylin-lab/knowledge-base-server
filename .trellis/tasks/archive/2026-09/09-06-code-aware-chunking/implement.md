# Implementation Plan

Order matters: regression tests land first and must fail for the documented
reason before any production edit.

## Step 1 — Failing regression tests (chunker)

`tests/test_chunker.py`, appended:

- `test_hash_comment_inside_fence_is_not_a_heading` — the P0-A reproducer.
- `test_fence_content_is_byte_preserved` — ` ```yaml ` + `# comment`, asserts
  no inserted blank line.
- `test_oversized_fence_splits_into_individually_valid_blocks` — every piece
  opens and closes its own fence with the original info string.
- `test_tilde_fence_and_long_backtick_fence_are_honored`.
- `test_short_closing_run_does_not_close_a_longer_fence`.
- `test_unclosed_fence_runs_to_end_of_document`.
- `test_heading_path_tracks_the_heading_stack` (structured API).
- `test_heading_path_is_not_prepended_to_chunk_text`.

Gate: `uv run pytest tests/test_chunker.py` fails on exactly these.

## Step 2 — Failing regression tests (ES analysis)

`tests/test_es_store.py` (`pytest.mark.es`, live node):

- `test_code_analyzer_keeps_programming_keywords` — `if`/`for`/`not` survive.
- `test_code_analyzer_splits_camel_case_and_snake_case` — `ConnectionPool`
  yields `connection`, `pool`, and the original.
- extend `test_ensure_index_creates_explicit_mapping_and_is_idempotent` with
  the `code` subfield and `heading_path`.

`tests/test_es_queries.py`:

- `test_bm25_query_targets_code_subfield_and_heading_path`.

## Step 3 — `rag/chunker.py`

1. `_FENCE_OPEN` / `_fence_closes` helpers + a small `_FenceState`.
2. Thread fence state through `_split_sections`, `_split_paragraphs`,
   `_split_by_lines`, `_split_oversized`.
3. Fence repair (close-then-reopen) at piece boundaries.
4. `Chunk` dataclass + heading stack; `chunk_markdown_structured`;
   `chunk_markdown` becomes the text-only wrapper.

Gate: Step 1 tests green, **all pre-existing chunker tests still green**.

## Step 4 — `search/es.py` + `search/queries.py`

1. `_CHUNK_SETTINGS` with the `code` analyzer; `ensure_index` passes
   `settings=` alongside `mappings=`.
2. `chunk_text.code` subfield + `heading_path` property.
3. `replace_document_chunks` accepts `Sequence[Chunk]`, indexes `heading_path`.
4. `bm25_chunk_query` fields + `_CODE_BOOST` / `_HEADING_BOOST`.

Gate: Step 2 tests green.

## Step 5 — `rag/indexer.py`

Switch to `chunk_markdown_structured`; build the embedding input as
`f"{title}\n{heading_path}\n\n{text}"`; keep PG storing `text`; pass chunks
through to ES. Update `ReplaceChunksFn` protocol and `tests/fakes.py` doubles.

Gate: `uv run pytest tests/test_indexer.py`.

## Step 6 — Full verification

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest
ES_INTEGRATION=1 uv run pytest -m es      # live-node leg
```

## Step 7 — Migration + docs

- README runbook + `search-guidelines.md` update (new analyzer, why the code
  subfield exists, the stopword incident).
- Migration: drop index → `UPDATE documents SET index_status='pending'` →
  `python -m app.cli reindex`.
- Re-probe `KB_SEARCH_VECTOR_MAX_DISTANCE` with
  `tests/test_vector_distance_probe.py` after reindex (embedding input changed).

## Rollback points

- After Step 3: chunker is self-contained; reverting it leaves ES untouched.
- After Step 4: mapping change requires a reindex either way; rolling back
  means dropping the index again.
