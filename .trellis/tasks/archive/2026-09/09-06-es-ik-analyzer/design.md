# Design: Add IK analyzer to Elasticsearch for Chinese corpus

## Architecture & boundaries

| Area | File(s) | Change |
|------|---------|--------|
| Infra | `docker/elasticsearch/Dockerfile` (new) | Official ES image + `analysis-ik` install, version via build ARG |
| Infra | `docker-compose.yml` | `elasticsearch` service gains `build:` + pinned `image:`; env/ports/volume/healthcheck unchanged |
| Code | `src/app/search/es.py` | `_CHUNK_MAPPINGS`: `title`/`chunk_text` gain `analyzer` + `search_analyzer` |
| Tests | `tests/test_es_store.py` | Mapping assertions extended; live `_analyze` plugin-contract test added |
| Docs | `README.md` | Prerequisites note (image is now built, not pulled) + upgrade runbook |

Explicitly **no changes**: `search/queries.py` (multi_match inherits the field
`search_analyzer`), `rag/indexer.py`, `core/config.py`, PG schema, and the CLI
(see D4).

## Key decisions

**D1 — Plugin installed from a pre-fetched zip (hermetic build); revised
during live verification.** The planned build-time URL install stalled in
practice: measured throughput to `get.infini.cloud` was ~1.6 KB/s (and the
GitHub repo publishes no version-tagged release assets, only an empty
`Latest` release), so a URL install is both slow and fragile here. Instead
the zip is fetched once into `docker/elasticsearch/` (ignored by git) and the
Dockerfile does `COPY` + `elasticsearch-plugin install file://…` — builds are
reproducible offline, and a cache-busting Dockerfile change no longer
re-downloads. The zip is version-locked to the ES build arg; compose pins
`ES_VERSION: "8.17.3"` and tags the image `kb-elasticsearch:8.17.3-ik`.
`release.infinilabs.com/analysis-ik/stable/` is the fetch channel (verified
HTTP 206 + zip integrity on 2026-09-06); `get.infini.cloud` remains the
alternative noted in the Dockerfile. A missing zip fails the build at `COPY`
with the README fetch command one paragraph away.

Rejected: runtime install via entrypoint wrapper (non-reproducible, restarts
re-download, container start order becomes network-dependent).

**D2 — `ik_max_word` at index time, `ik_smart` at search time.**
The standard IK pairing: fine-grained segmentation maximizes indexed terms
(recall), coarse-grained query segmentation avoids query-term explosion and
keeps precision. Applied uniformly to `title` and `chunk_text`. The mapping
is the single place analyzers are declared — the "explicit mapping beats
dynamic" comment in `es.py` stays true.

**D3 — Analyzers hardcoded, not Settings-configurable (deferred escape
hatch).** The project ships its own compose ES *with* the plugin, so the
shipped config always satisfies the mapping. A hypothetical external ES
without IK fails fast and loudly at the first `ensure_index`
(`analyzer_not_found` → wrapped `SearchIndexError`), which is better than
silently keeping single-character Chinese search. If a plugin-less external
ES deployment ever becomes real, add `ES_INDEX_ANALYZER`/`ES_SEARCH_ANALYZER`
settings with IK defaults at that point.

**D4 — Migration composes existing primitives; no new CLI surface.**

The bootstrap decision "full rebuild not offered by the CLI" stays intact.
Existing installs migrate via ops steps only:

1. `docker compose build elasticsearch && docker compose up -d`
2. `curl -XDELETE localhost:9200/kb_documents` (drop stale-mapping index)
3. `UPDATE documents SET index_status = 'pending'
   WHERE index_status = 'done';` — PG enum type `index_status`, lowercase
   values, per `src/app/models/document.py:49-56`
4. `uv run python -m app.cli reindex` (default limit 50; repeat until
   `processed: 0` for larger corpora)

The first swept document triggers `ensure_index`, which recreates
`kb_documents` with the IK mapping; `process_document` re-chunks, re-embeds,
and rewrites with deterministic doc ids — idempotent, and safe to re-run.

Rejected alternative: ES `_reindex` + alias swap (no re-embedding cost, but
introduces an alias concept the fixed-name `ES_INDEX` world doesn't have, and
one-off ops API calls with no home in the repo). At this corpus scale,
re-embedding through the user's own endpoint is cheap; simplicity wins.

## Data flow / contracts

- `ensure_index` → `indices.create(mappings=_CHUNK_MAPPINGS)` keeps its shape;
  two text fields gain two keys. New vs old deployments diverge only in the
  created mapping, never in code paths.
- Write path (`replace_document_chunks`) and deterministic doc ids unchanged
  → re-runs are idempotent.
- Read path: `multi_match` over `chunk_text`/`title^2` inherits `ik_smart`
  per field; `min_score` gates keep working (scores change in absolute terms —
  gates are relative floors set in settings, re-tunable if needed later).

## Compatibility & rollback

- Fresh installs: nothing to do — compose build produces the IK image.
- Existing installs: runbook above (~minutes at this scale; dev data only).
- Rollback: `git revert` the mapping change → drop index → rerun steps 3-4 of
  the runbook. The compose build ARG pin reproduces the plugin-less image by
  checking out the previous compose file.

## Risks

- Plugin zip must be fetched before the first build (`curl` one-liner in
  README); a missing zip fails the build at `COPY` — loud, not silent. The
  zip is git-ignored, so it is a per-machine, one-time fetch.
- BM25 absolute scores shift after re-analysis; the retriever's min-score
  gates are settings-driven floors (`KB_SEARCH_*`) — unchanged by default,
  re-tunable if search quality feels different.
- Single-node ES reports yellow (replicas unassigned) — pre-existing, unrelated.
