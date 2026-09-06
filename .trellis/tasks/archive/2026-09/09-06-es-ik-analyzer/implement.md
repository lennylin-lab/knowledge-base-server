# Implementation plan: Add IK analyzer to Elasticsearch

Ordered checklist. Steps 1-3 are file edits (independent rollback points);
step 4 is live verification against the local deployment; step 5 is the
quality gate.

## Checklist

1. [x] `docker/elasticsearch/Dockerfile` (new) — REVISED during live
   verification: build-time URL install stalled at ~1.6 KB/s against
   get.infini.cloud (GitHub publishes no version-tagged assets). Final form:
   `COPY elasticsearch-analysis-ik-*.zip` + `elasticsearch-plugin install
   file:///tmp/analysis-ik.zip` — hermetic, offline-reproducible builds. The
   zip is fetched once per machine (README one-liner; release.infinilabs.com,
   integrity + sha256 verified) and is git-ignored.
2. [ ] `docker-compose.yml`
   - `elasticsearch` service gains
     `build: { context: ./docker/elasticsearch, args: { ES_VERSION: "8.17.3" } }`
     and `image: kb-elasticsearch:8.17.3-ik`
   - env / ports / volume / healthcheck untouched
3. [ ] `src/app/search/es.py` — `_CHUNK_MAPPINGS`
   - `title`, `chunk_text`: `"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"`
   - extend the mapping doc-comment to name the analyzers and why (CJK word
     segmentation; ik_smart at query time)
4. [ ] `tests/test_es_store.py`
   - extend `test_ensure_index_creates_explicit_mapping_and_is_idempotent`:
     assert `analyzer == "ik_max_word"` and `search_analyzer == "ik_smart"` on
     both text fields
   - add one live test: `_analyze` with `analyzer: ik_max_word` on a Chinese
     sentence (e.g. 知识库检索) asserts ≥2 tokens and every token length ≥ 2
     (fails when the plugin is missing → pins the plugin contract)
   - follow the existing `es` marker convention
5. [ ] Live verification on the local deployment (runbook, also lands in README)
   - `docker compose build elasticsearch && docker compose up -d`
   - `docker compose exec elasticsearch elasticsearch-plugin list` → `analysis-ik`
   - `curl -XDELETE localhost:9200/kb_documents`
   - `docker compose exec postgres psql -U kb -d kb -c "UPDATE documents SET index_status = 'pending' WHERE index_status = 'done';"`
   - `uv run python -m app.cli reindex` → confirm `processed: 40, done: 40`
     (repeat until `processed: 0` if more)
   - `curl -s localhost:9200/kb_documents/_count` → 40 restored
6. [ ] README.md
   - setup section: note that ES is now built locally (first `up` compiles the
     image; needs network for the plugin download)
   - new "Upgrading an existing installation to the IK analyzer" runbook
     (steps of 5, framed for existing-data installs)
7. [ ] Quality gate (step 2.2 / last-iteration full-scope check)
   - `uv run ruff check .`
   - `uv run mypy`
   - `uv run pytest` — ES-marked tests must RUN (local ES reachable), not skip

## Validation commands

```bash
docker compose build elasticsearch
docker compose up -d elasticsearch
docker compose exec elasticsearch elasticsearch-plugin list
uv run python -m app.cli reindex
uv run ruff check . && uv run mypy && uv run pytest
```

## Risky files / rollback points

- `docker-compose.yml` is the deployment entry point — a broken build arg
  blocks `up` for everyone; verify `build` + `up` before moving on (step 5).
- `src/app/search/es.py` changes the index contract; rollback = revert the
  constant + drop index + rerun runbook steps (recreate with old mapping).
- Migration step 3 is a bulk status reset — dev data only; harmless to repeat
  (pipeline is idempotent), never run against shared infra.

## Pre-start checks

- `implement.jsonl` / `check.jsonl` carry real spec entries.
- PRD/design/implement reviewed by the user (final planning summary approved).
