# Search Layer Guidelines

> Elasticsearch contract for `knowledge-base-server`: image build, mapping,
> analyzers, index lifecycle. Established 2026-09-06 (task
> `09-06-es-ik-analyzer`); code analyzer + heading breadcrumbs added same day
> (task `09-06-code-aware-chunking`).

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

**What**: index `settings` (analysis config) and `mappings` live together in
`search/es.py` — `_CHUNK_SETTINGS` declares the analyzers, `_CHUNK_MAPPINGS`
binds them to fields. `title`/`chunk_text`/`heading_path` are `{"type":
"text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"}`;
`keyword`/`integer` fields stay analyzer-free; `ensure_index` passes
`settings=` alongside `mappings=`. Queries (`search/queries.py`) name fields
and boosts only — never an `analyzer` key.

**Why (primary fields)**: fine-grained index-time segmentation maximizes
recall; coarse query-time segmentation avoids query-term explosion. One
declaration site keeps the "explicit mapping beats dynamic" invariant
auditable.

**Why (the `code` subfield — stopword incident, 2026-09-06)**: `analysis-ik`
ships an English stopword list, measured against the live node:

```
ik_smart("how to use async_bulk with AsyncElasticsearch for a loop if not null")
  -> [how, use, async_bulk, asyncelasticsearch, loop, null]
  dropped: to, with, for, a, if, not
```

`if`/`for`/`not`/`with`/`in`/`is`/`or`/`as` are content words in a
programming corpus — queries like "for 循环怎么写" or "if not None 判断" lose
their most discriminative terms. IK also never splits identifiers
(`ConnectionPool` stays one token, so `connection pool` can never match it).
The escape hatch is a multi-field, NOT a replacement analyzer: `chunk_text`
keeps IK (Chinese word segmentation is the corpus majority) and
`chunk_text.code` analyzes with the index-settings `code` analyzer —
whitespace tokenizer (no stopword list: keywords survive) +
`word_delimiter_graph` (`preserve_original`, `split_on_case_change`,
`catenate_words`, `split_on_numerics=False`) + `flatten_graph` + `lowercase`.
One analyzer for index and search keeps the declaration single-site;
`flatten_graph` is mandatory because index-time analyzers cannot emit token
graphs. In `best_fields` a term matched by both `chunk_text` and its `code`
subfield scores once (max, not sum) — the subfield adds recall only.

**Heading breadcrumbs**: every ES chunk doc carries `heading_path` (the
ancestor breadcrumb from `rag/chunker.py::Chunk`, IK-analyzed like `title`),
boosted mildly in `bm25_chunk_query`. It is retrieval signal only: PG stores
the plain chunk text and search returns that text unmodified as `content`;
the vector leg embeds `title + heading_path + text` (`rag/indexer.py
::embedding_input`) but nothing user-visible changes shape.

> **Warning**: analyzer changes never apply to existing indexes (Elasticsearch
> validates mappings, it does not re-analyze in place) — and chunking changes
> alter embeddings too. The only migration is drop index + reset
> `documents.index_status` to `'pending'` (PG enum `index_status`, lowercase
> values) + `python -m app.cli reindex` — the CLI deliberately offers no
> full-rebuild flag; ops composes primitives. The runbook (with the list of
> migrations so far) lives in README.md.

---

**Language**: All documentation is written in **English**.
