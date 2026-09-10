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
# queries.py stays analyzer-free; every clause inherits each field's
# analyzers from _CHUNK_MAPPINGS (single declaration site).
{"bool": {"should": [
    {"multi_match": {"query": q, "fields": ["title^2", "heading_path^1.5"],
                     "type": "best_fields",
                     "minimum_should_match": "70%"}},  # identity group: max, not sum
    {"match": {"chunk_text": {"query": q, "minimum_should_match": "70%"}}},
    {"match": {"chunk_text.code": {"query": q, "boost": 1.5}}},
], "minimum_should_match": 1}}
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
`chunk_text.code` uses TWO analyzers (2026-09-08, task
`09-08-es-bm25-scoring`) — both still declared in `_CHUNK_SETTINGS` only:

- **index** `code`: whitespace tokenizer (no stopword list: keywords
  survive) + `word_delimiter_graph` (`preserve_original`,
  `split_on_case_change`, `catenate_words`, `split_on_numerics=False`) +
  `flatten_graph` (mandatory: index-time analyzers cannot emit token
  graphs) + `lowercase` + `remove_duplicates`.
- **search** `code_search`: whitespace + a delimiter filter with
  `preserve_original=False`, `catenate_words=False` (+`lowercase`) — a
  flat, unstacked stream. NO `flatten_graph` (flattening is an index-time
  requirement, not a search-time one).

`remove_duplicates` (D3): `preserve_original` + `catenate_words` emit the
SAME string twice at one position for pure camelCase
(`setState -> setstate(p0) setstate(p0) set(p0) state(p1)`), inflating tf
to 2. It runs after `lowercase` so casing variants collapse first.

> **Warning (D2 — the phrase-query trap)**: a `word_delimiter_graph` with
> `preserve_original`/`catenate_words` used as a SEARCH analyzer stacks
> alternatives at one position; Lucene compiles the stack into an
> adjacency-constrained phrase, so identifier queries returned **0 hits**
> (`ConnectionPool`, `async_bulk` measured). Rule: index-time expansion
> filters never run at search time — split analyzers and bind the search
> one via `search_analyzer` in `_CHUNK_MAPPINGS`. `auto_generate_synonyms
> _phrase_query: false` at the query site was rejected: it patches the
> symptom, leaves the broken analyzer in place, and drags analyzer
> knowledge into `queries.py` against C3.

Identifier matching is symmetric through sub-words after the split:
`ConnectionPool` and `connection pool` both analyze to `[connection,
pool]` on the search side and match the indexed parts. The exact
concatenated form (`connectionpool`) is no longer a distinct query term on
the code subfield — accepted trade-off: a chunk with "connection" and
"pool" scattered apart scores like one containing the identifier; recall
was the priority (D2 measured 0 hits for identifier queries before this).

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

## Scenario: BM25 scoring shape and the coverage gate

### 1. Scope / Trigger

- Trigger: cross-layer contract change — `bm25_chunk_query` body shape and
  a new `Settings` env key (`KB_SEARCH_BM25_MIN_COVERAGE`), added
  2026-09-08 (task `09-08-es-bm25-scoring`).

### 2. Signatures

- `bm25_chunk_query(q, *, size, tag=None, min_score=0.0,
  min_coverage=DEFAULT_BM25_MIN_COVERAGE)` in `search/queries.py`.
- `Retriever(..., bm25_min_coverage=...)` — constructor-injected from
  `Settings` via `_build_retriever` (established gates pattern).

### 3. Contracts

- Body: three `bool.should` groups — identity (`title^2` + `heading_path^1.5`
  as ONE `best_fields` group, max within), prose (`chunk_text`), code
  (`chunk_text.code^1.5`); independent groups SUM; outer
  `minimum_should_match: 1`; tag filter as `filter` clause.
- `KB_SEARCH_BM25_MIN_COVERAGE` (default `"70%"`, `""` omits the key):
  ES `minimum_should_match` on the `chunk_text` leaf AND the
  title/heading identity group (both IK-tokenized, so a percentage of the
  query's terms is well-defined). Identity-group coverage added 2026-09-10
  (task `09-10-irrelevant-query-noise-gates`): without it a single
  function-word title hit (a lone "的") satisfied the outer
  `minimum_should_match: 1` and activated the whole BM25 leg above genuine
  prose evidence. ES rounds the percentage DOWN per field, so single-term
  identifier queries are unaffected.
- `KB_SEARCH_BM25_MIN_SCORE` (default `0.0`, **retired**): mechanism and
  ES-side `min_score` plumbing kept as an operator escape hatch only.

### 4. Validation & Error Matrix

- `heading_path` contains `title` textually (C1) → any additive scheme
  must keep them in one max-group, else title matches triple-count.
- Absolute BM25 floor re-enabled → gate silently never fires or silences
  real hits: top-hit scores measured 4.46–28.39 across probe queries,
  min/top ratios 0.045–0.586 (D5) — the reason the default is 0.0.
- Coverage tightened to `100%` → legitimate queries return 0 (`for
  循环怎么写` measured); `70%` is the loosest value that zeroes the noise
  probes.

### 5. Good/Base/Bad Cases

- Good: `setState 状态管理` — chunk matching both halves outranks
  single-field matches (D1 inversion fixed; measured 39.99/30.99 vs 15.67
  on the 09-08 corpus; re-anchored 2026-09-10 on the current corpus: React
  渲染与并发 both-halves 18.84 (rank 1) vs React title-only 16.03 (rank 4)
  — note the Flutter root chunk now mentions setState in its body, so it is
  a both-halves chunk, not a single-side anchor).
- Base: pure-CJK query — identity + prose groups carry it, code group
  contributes nothing.
- Bad: setting `minimum_should_match` on `chunk_text.code` (different
  token semantics — that analyzer keeps IK-dropped stopwords) or REMOVING
  it from the identity group (a lone stopword title hit then activates
  the whole BM25 leg — the 09-10 noise hole; before 2026-09-10 the spec
  said the opposite — the "penalizes short breadcrumbs" worry is bounded
  by per-field round-down: one-term breadcrumbs are always fully
  required, and multi-term breadcrumbs need the same 70% the prose leaf
  needs).

### 6. Tests Required

- `tests/test_es_queries.py`: full-dict body pin; coverage present on
  the prose leaf AND the identity group / omitted on `""` (both);
  recursive no-`analyzer`-key walk; tag-filter non-interference
  (offline).
- `tests/test_es_relevance.py` (`es`-marked, live): D2 identifier
  regressions (`useState`/`lru_cache` ≥ 1 hit — re-anchored 2026-09-10 from
  `ConnectionPool`/`async_bulk` when the dev corpus was replaced; one anchor
  per naming convention), D2 plain-words presence (`use state`/`lru cache`
  must reach the identifier chunks; totals NOT pinned — corpus-mutable),
  D1 rank ordering, C1 title non-duplication (synthetic corpus), noise → 0,
  relevance probes ≥ 1.
- `tests/test_search_gates.py`: Settings↔query-builder↔Retriever
  drift-guard covers `SEARCH_BM25_MIN_COVERAGE` and the 0.0 score default.

### 7. Wrong vs Correct

#### Wrong

```python
# best_fields takes ONE field's score; cross-field evidence is discarded
# (rank inversion: mixed queries lose to single-field matches).
{"multi_match": {"query": q, "fields": _MATCH_FIELDS}}
```

#### Correct

```python
# Groups that must not double-count share a max-group; independent
# evidence sums. See §2/§3 above for the exact shape.
```

**Calibration record**: design.md § Calibration of task
`09-08-es-bm25-scoring` holds the before/after probe matrix (9 queries ×
coverage values) — re-run the probe before changing coverage or boosts.
The vector rescue on-domain trigger
(`KB_SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE`, default 0.62, added
2026-09-10) is calibrated in design.md § Calibration of task
`09-10-irrelevant-query-noise-gates` (real 15-doc corpus: in-domain
short-keyword leg_min 0.473-0.652 vs off-domain 0.656-0.774 — the bands
touch; 0.62 is a precision-first policy choice, losing only bare `事务`)
— re-run `uv run pytest -m live_llm tests/test_vector_distance_probe.py -s`
before changing it. Since 2026-09-10 (task
`09-10-vector-rescue-bm25-backstop`) the rescue tier additionally requires
BM25-leg emptiness (scenario below), so trigger-calibration probes must use
BM25-empty queries — with BM25 survivors rescue is suppressed and the
trigger is unreachable.

---

## Scenario: Vector rescue is a lexical-failure backstop

### 1. Scope / Trigger

- Trigger: cross-layer retrieval contract change —
  `filter_vector_rows_with_rescue` gained the `bm25_leg_empty` precondition
  (2026-09-10, task `09-10-vector-rescue-bm25-backstop`). Any change to the
  rescue precondition chain must re-verify this scenario.

### 2. Signatures

- `rag/retriever.py`:
  `filter_vector_rows_with_rescue(rows, *, max_distance, rescue_margin,
  rescue_max_distance, rescue_trigger_max_distance, bm25_leg_empty)
  -> tuple[list[ChunkRow], int]`.
- Call site: `Retriever.retrieve` passes
  `bm25_leg_empty=(len(kept_es_hits) == 0)` — zero survivors AFTER
  `filter_es_hits` (the gated BM25 leg), never a raw/pre-gate hit count.
  `kept_es_hits` is already computed above the vector gate; both legs are
  gathered before this point, so no reordering or extra awaits.

### 3. Contracts

- Full rescue precondition chain (all must hold, in evaluation order):
  GATES_OFF off (`max_distance < 2.0`) → primary tier empty AND rows exist
  → BM25 leg empty → rescue enabled (`margin > 0`, `cap > 0`) → on-domain
  (`leg_min <= KB_SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE`, 0.62) →
  window `min(leg_min + margin, cap)`.
- "BM25 empty" = zero gated survivors — deliberately scale-free; a count
  threshold ("weak" ≤ N) would be a new corpus/limit-dependent knob and is
  rejected.
- The returned count is rows admitted ONLY via rescue — 0 whenever the
  primary tier survives or the BM25 leg is non-empty;
  `search_executed.vector_rescued` inherits exactly this meaning.
- The coupling has NO Settings key: it is structural (cross-leg logic the
  retriever alone can see), not an operator-tunable calibration. Adding one
  would break the Settings↔`DEFAULT_*` drift-guard shape for no benefit.

### 4. Validation & Error Matrix

- Relevant and irrelevant chunks INTERLEAVE in vector distance inside a
  topical cluster (`q=python`: genuine Python chunk at 0.687 is farther
  than the Vue leak at 0.660) → no vector-side threshold or window
  tightening separates them; cross-leg agreement is the only clean
  separator. Do not re-attempt vector-only fixes.
- Raising `SEARCH_RRF_MIN_RELATIVE` to kill single-leg leaks → global and
  blunt, cuts genuine single-leg tails in mixed sets; rejected (see task
  design.md § Tradeoffs). Confidence-weighted RRF remains the deferred
  general solution.
- Wiring the BM25 condition inside `filter_vector_rows` → wrong layer: the
  primary tier is pure distance filtering; cross-leg knowledge enters only
  via the rescue helper's flag.

### 5. Good/Base/Bad Cases

- Good: `q=python` — BM25 ranks 6 genuine chunks, primary vector tier
  emptied (leg_min 0.586 > 0.45 ceiling, ≤ 0.62 trigger) → rescue
  suppressed, BM25-only fusion, zero topical-neighbor leaks
  (live-verified 2026-09-10).
- Base: vocabulary-mismatch short keyword (`q=coroutine`-shaped) — BM25 0
  survivors, on-domain leg → rescue fires exactly as before; recall
  preserved (live-verified 2026-09-10).
- Bad: computing `bm25_leg_empty` from pre-gate ES hits or an `es_score`
  heuristic, or "restoring" rescue by defaulting the flag to `True` in the
  helper body instead of at the call site (rollback stays a call-site
  decision).

### 6. Tests Required

- `tests/test_search_gates.py`: rescue suppressed when
  `bm25_leg_empty=False` despite emptied primary tier + on-domain leg_min;
  every prior rescue case passes `bm25_leg_empty=True` (bit-identical
  behavior pin).
- `tests/test_retriever.py`: BM25-non-empty + emptied primary vector →
  `vector_rescued == 0` and no vector-only results; BM25-empty short
  keyword → still rescued.
- Task 09-10 out-of-domain regressions stay green: off-domain + BM25-empty
  → trigger blocks rescue unchanged (the new condition only tightens
  further).

### 7. Wrong vs Correct

#### Wrong

```python
# Rescue decides from vector-leg evidence alone: any in-domain keyword
# query whose primary tier empties gets the whole same-domain cluster.
if measured_min <= rescue_trigger_max_distance:
    ...  # fire rescue
```

#### Correct

```python
# Rescue is the backstop for LEXICAL failure: without an empty gated BM25
# leg it never fires, whatever the vector distances look like.
if not bm25_leg_empty:
    return [], 0
```

---

## Scenario: History-aware query rewrite precedes retrieval (chat path)

### 1. Scope / Trigger

- Trigger: cross-layer contract change — new `Settings` env keys, new
  `ChatService` constructor params, and a new agent module
  (`agents/rewrite.py`; 2026-09-10, task
  `09-10-query-rewrite-followups`). Any change to what query reaches the
  retriever on chat follow-up turns must re-verify this scenario.

### 2. Signatures

- `agents/rewrite.py::build_rewrite_agent(model: Model) -> Agent[None, str]`
  — toolless (`deps_type` defaults to `None`), instructions from
  `agents/prompts/rewrite.md` via the shared `qa.load_prompt`; imports
  pydantic-ai + the prompt loader only (layering convention #6).
- `ChatService(..., *, rewrite_model: Model | None = None,
  rewrite_history_turns: int = 3)` — both keyword-only, default-off, so
  pre-existing call sites are unchanged.
- `ChatService._rewrite_query(question, history) -> str` — best-effort,
  never raises.
- `api/deps.py::build_chat_service` passes
  `rewrite_model=model if settings.CHAT_QUERY_REWRITE_ENABLED else None`
  (the SAME `get_chat_model` instance — one SDK client per process) and
  `rewrite_history_turns=settings.CHAT_REWRITE_HISTORY_TURNS`.

### 3. Contracts

- The rewritten string is ONLY the `run_stream` prompt. The ORIGINAL question
  is what `_prepare_turn` persists and what `to_message_history` rebuilds for
  later turns — history stays faithful to what the user typed. pydantic-ai
  copies `message_history` on `run`/`run_stream` (verified in installed
  source), so passing `turn.history` to both the rewrite run and the QA run
  cannot mutate shared state.
- Gate placement: first turn (empty history), stateless construction (no
  factory), and flag-off (`rewrite_model=None`) short-circuit before any
  rewrite LLM call or new log line — byte-identical to pre-rewrite behavior,
  `RunStartedEvent` timing included.
- The disable switch is `KB_CHAT_QUERY_REWRITE_ENABLED` folded into
  construction (model injected or not), NOT a runtime branch in `ask`; the
  runtime kill switch is setting it false (no deploy).
- Turn cap: `history[-2 * rewrite_history_turns:]` (N turns = 2N messages);
  `<= 0` uses the full assembled window
  (`KB_CHAT_REWRITE_HISTORY_TURNS`, default 3).
- Prompt contract (`rewrite.md`): resolve pronouns/anaphora/ellipsis from the
  history; return the question UNCHANGED when already self-contained;
  preserve the original language; output only the query text.
- Log events: `query_rewrite` info (emitted only when a rewrite is
  attempted) carries `applied`/`changed`/`original_length`/
  `rewritten_length`; `query_rewrite_failed` carries `error_class` only.
  NEVER the question or rewritten text (logging-guidelines).

### 4. Validation & Error Matrix

- Rewrite agent raises (provider error, etc.) → caught →
  `query_rewrite_failed` (error_class only) → raw question proceeds; the
  stream still reaches `DoneEvent`. Degrade-to-raw is the mandated failure
  policy (never a terminal error solely from the rewrite step); the
  streaming rule's BaseException carve-out holds —
  `CancelledError`/`GeneratorExit` propagate for cancellation.
- Empty/whitespace rewrite output → raw question.
- Rewriting inside `search_knowledge` via `ChatDeps` → wrong layer
  (rejected): an LLM round-trip per search call, a retrieval tool growing an
  LLM dependency, and the answer-generation prompt stays anaphoric.

### 5. Good/Base/Bad Cases

- Good: turn 2 `那它的缺点呢?` after a topic turn → the retriever receives a
  standalone Chinese query with the referent resolved (language preserved,
  pinned via `StubRetriever.calls`).
- Base: already self-contained follow-up → the prompt's no-op rule returns
  it unchanged (pass-through, not a paraphrase).
- Bad: persisting or history-rebuilding the rewritten form (breaks the
  faithful-history contract); rewriting on the first turn or in the
  stateless path (breaks the byte-identical invariant); a bool flag instead
  of the model-or-None param (a bool would still need a model for tests).

### 6. Tests Required

- `tests/test_chat_service.py` (AC1–AC7): the standalone query reaches the
  retriever AND is the run prompt itself (`_user_prompts` on the recorded QA
  histories == [original turn-1, standalone turn-2] — with `message_history`,
  the run's own prompt is the LAST user entry); first turn / stateless /
  flag-off never invoke the rewriter; persisted rows and next-turn history
  carry the ORIGINAL text; a scripted rewrite failure degrades to raw with
  `DoneEvent` and no `ErrorEvent`; no question/rewritten text in captured
  logs.

### 7. Wrong vs Correct

#### Wrong

```python
# History-blind retrieval: the anaphoric follow-up reaches the retriever
# verbatim (the QA model's own search query is not reliably self-resolving).
async with self._agent.run_stream(question, deps=deps, message_history=history):
```

#### Correct

```python
# Service-level, pre-run, best-effort: the run prompt is self-contained;
# persistence and history keep the original.
retrieval_question = await self._rewrite_query(question, turn.history if turn else [])
async with self._agent.run_stream(
    retrieval_question, deps=deps,
    message_history=turn.history if turn and turn.history else None,
):
```

---

**Language**: All documentation is written in **English**.


