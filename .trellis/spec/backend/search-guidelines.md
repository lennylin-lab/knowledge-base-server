# Search Layer Guidelines

> Elasticsearch contract for `knowledge-base-server`: image build, mapping,
> analyzers, and index lifecycle. Established 2026-09-06 (task
> `09-06-es-ik-analyzer`).

---

## Scenario: ES image + IK analyzer plugin

### 1. Scope / Trigger

- Trigger: infra integration — the compose ES image is *built*, not pulled,
  and bakes in the `analysis-ik` plugin. Any ES work depends on this setup.

### 2. Signatures

- Compose build (single source of the ES version):

  ```yaml
  elasticsearch:
    build:
      context: ./docker/elasticsearch
      args: { ES_VERSION: "8.17.3" }
    image: kb-elasticsearch:8.17.3-ik
  ```

- Plugin install is hermetic: the version-locked zip is fetched into
  `docker/elasticsearch/` (git-ignored) *before* `docker compose build`:

  ```bash
  curl -fL --retry 5 -o docker/elasticsearch/elasticsearch-analysis-ik-8.17.3.zip \
    https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-8.17.3.zip
  ```

  Dockerfile: `COPY elasticsearch-analysis-ik-*.zip` →
  `elasticsearch-plugin install --batch "file:///tmp/analysis-ik.zip"`.

### 3. Contracts

- **Version lock**: plugin artifact version == `ES_VERSION` build arg == base
  image tag. The installer validates `elasticsearch.version` compatibility at
  build time, so drift fails the build (never a silently-wrong image).
- **Bumping the ES version** touches exactly two places: the compose build
  arg and the refetched zip filename/URL.
- `get.infini.cloud` is an alternative distribution channel but measured
  ~1.6 KB/s on this network; `release.infinilabs.com` is the fetch default.
  The GitHub repo publishes NO version-tagged release assets.

### 4. Validation & Error Matrix

- Missing zip in build context -> `COPY` fails the build (README fetch
  command is the fix).
- Plugin absent / wrong ES version at `RUN elasticsearch-plugin install` ->
  build fails with the installer's compatibility error.
- Analyzer missing at runtime (external ES without IK) -> first
  `ensure_index` raises `analyzer_not_found`, wrapped as `SearchIndexError`
  (`operation`/`index`/`error_class` only) — loud, by design (D3).

### 5. Good/Base/Bad Cases

- Good: fresh clone → fetch zip → `docker compose up -d` → index created
  with IK analyzers.
- Base: existing install → README runbook (drop index, reset
  `done→pending`, `python -m app.cli reindex`).
- Bad: setting `analyzer` in query bodies, or editing mappings anywhere but
  `search/es.py::_CHUNK_MAPPINGS`.

### 6. Tests Required

- `tests/test_es_store.py::test_ensure_index_creates_explicit_mapping_and_is_idempotent`
  asserts the full property dict incl. `analyzer`/`search_analyzer` (live).
- `test_analyze_with_ik_max_word_segments_chinese_into_words` pins plugin
  presence: standard analyzer emits ONLY single chars for CJK, so a
  multi-char token (`"智能" in tokens`) proves word segmentation. ik_max_word
  legitimately also emits single chars — do NOT assert `all(len(t) >= 2)`.

### 7. Wrong vs Correct

#### Wrong

```python
# Declaring analyzers at the query site — splits the declaration in two.
{"multi_match": {"query": q, "fields": ["chunk_text"], "analyzer": "ik_smart"}}
```

#### Correct

```python
# queries.py stays analyzer-free; multi_match inherits each field's
# search_analyzer declared in _CHUNK_MAPPINGS (single declaration site).
{"multi_match": {"query": q, "fields": ["chunk_text", f"title^{_TITLE_BOOST}"]}}
```

---

## Convention: Mapping is the only analyzer declaration site

**What**: `title`/`chunk_text` are `{"type": "text", "analyzer":
"ik_max_word", "search_analyzer": "ik_smart"}` in `search/es.py
::_CHUNK_MAPPINGS`; `keyword`/`integer` fields stay analyzer-free.

**Why**: fine-grained index-time segmentation maximizes recall; coarse
query-time segmentation avoids query-term explosion. One declaration site
keeps the "explicit mapping beats dynamic" invariant auditable.

> **Warning**: analyzer changes never apply to existing indexes (Elasticsearch
> validates mappings, it does not re-analyze in place). The only migration is
> drop index + reset `documents.index_status` to `'pending'` (PG enum
> `index_status`, lowercase values) + `python -m app.cli reindex` — the CLI
> deliberately offers no full-rebuild flag; ops composes primitives.

---

**Language**: All documentation is written in **English**.
