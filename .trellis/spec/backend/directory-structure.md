# Directory Structure

> How backend code is organized in this project.

---

## Overview

- **Stack**: Python 3.12+ / FastAPI (fully async) + Pydantic AI agents +
  PostgreSQL (SQLAlchemy 2.0 async + pgvector) + Elasticsearch +
  MCP client (`mcp` SDK) + OpenAI-compatible LLM access.
- **Layout**: `src/` layout with a single top-level package `app`.
- **Layering**: `router → service → agent → (rag | mcp | repository)`,
  see matrix below.
- **Package manager**: uv (`pyproject.toml` + `uv.lock` are the source of truth).

This workspace (`knowledge-base-server`) is **backend-only**. Frontend code
lives in a separate repository; do not create frontend directories here.

---

## Directory Layout

```
knowledge-base-server/
├── pyproject.toml            # deps, ruff/mypy/pytest config, project metadata
├── uv.lock                   # lockfile — always commit
├── alembic.ini
├── alembic/
│   └── versions/             # one migration file per revision
├── src/app/
│   ├── main.py               # create_app() factory; no business logic
│   ├── core/
│   │   ├── config.py         # pydantic-settings Settings (env-driven)
│   │   ├── database.py       # async engine + sessionmaker + get_db dependency
│   │   ├── exceptions.py     # app exception hierarchy (see error-handling.md)
│   │   └── logging.py        # structlog configuration
│   ├── models/               # SQLAlchemy ORM models (tables)
│   │   └── document.py
│   ├── schemas/              # Pydantic request/response DTOs
│   │   └── document.py
│   ├── repositories/         # PostgreSQL data access (DB queries only)
│   │   └── document.py
│   ├── services/             # business logic / orchestration
│   │   ├── document.py
│   │   └── chat.py
│   ├── agents/               # Pydantic AI agent definitions
│   │   ├── qa.py             # QAAgent: knowledge-grounded answering
│   │   ├── summarize.py
│   │   ├── association.py    # knowledge-link analysis
│   │   ├── writing.py        # assisted authoring
│   │   └── prompts/          # prompt templates (markdown, versioned assets)
│   ├── llm/                  # provider abstraction (no domain logic)
│   │   ├── models.py         # Pydantic AI model factory from Settings
│   │   └── embeddings.py     # EmbeddingProvider protocol + OpenAI-compat impl
│   ├── rag/                  # retrieval-augmented generation pipeline
│   │   ├── chunker.py        # markdown-aware chunking
│   │   ├── indexer.py        # doc -> chunks -> embeddings -> stores
│   │   ├── worker.py         # ARQ task fn + WorkerSettings (queue mode)
│   │   └── retriever.py      # ES BM25 + pgvector, RRF fusion
│   ├── mcp/                  # MCP extension mechanism
│   │   ├── manager.py        # server lifecycle (stdio/HTTP), tool discovery
│   │   └── tools.py          # MCP tools wrapped for agent registration
│   ├── search/               # Elasticsearch access layer
│   │   ├── es.py             # client from Settings, index lifecycle
│   │   └── queries.py        # BM25 query builders
│   ├── api/
│   │   ├── deps.py           # shared FastAPI dependencies
│   │   └── v1/
│   │       ├── router.py     # aggregates endpoint routers for v1
│   │       └── endpoints/
│   │           ├── documents.py
│   │           └── chat.py   # SSE streaming endpoint
│   └── utils/                # pure helpers with no I/O
└── tests/
    ├── conftest.py           # async fixtures, test DB, client factory
    ├── test_documents_api.py
    └── test_chat_api.py
```

---

## Module Organization

### Layering rules (strict)

| Layer | Directory | May import | Must not import |
|-------|-----------|-----------|-----------------|
| Router | `api/` | `services/`, `schemas/`, `api/deps.py` | `models/`, `repositories/`, `agents/`, SQLAlchemy |
| Service | `services/` | `repositories/`, `models/`, `schemas/`, `agents/`, `rag/`, `core/` | `api/`, FastAPI objects (`Request`, `Response`) |
| Agent | `agents/` | `llm/`, `rag/`, `mcp/`, `schemas/`, `core/` | `api/`, `services/`, FastAPI objects |
| RAG | `rag/` | `models/`, `repositories/`, `search/`, `llm/`, `core/`, `arq` (queue infra in `rag/worker.py` only) | `api/`, `services/`, `agents/` |
| MCP | `mcp/` | `core/`, `schemas/`, `pydantic-ai` (tool wrapping in `mcp/tools.py` only) | `api/`, `services/`, `agents/`, `models/`, FastAPI |
| LLM | `llm/` | `core/` | everything domain (`services/`, `agents/`, `rag/`, …) |
| Repository | `repositories/` | `models/`, `core/database.py` | `services/`, `api/`, `agents/`, Pydantic schemas |

Key points:

- **Services drive agents, never the reverse.** A service picks the agent,
  builds its run context, and registers extra tools as closures — that is how
  service capabilities reach an agent without `agents/` importing `services/`
  (prevents import cycles). Intra-`services/` imports of **pure domain
  helpers** are allowed (`services/chat.py` ← `derive_title` from
  `services/session.py`): the matrix constrains cross-layer deps, not
  same-layer reuse — keep it acyclic and pure.
- **`llm/` is a pure provider layer**: model instances, embedding clients,
  retries/timeouts. It knows nothing about documents or knowledge. All model
  names, `base_url`, API keys come from `Settings` — nothing hardcoded.
- **`rag/` owns the retrieval pipeline**: chunking, embedding, hybrid search
  (ES BM25 + pgvector cosine), RRF fusion. `search/` and `repositories/` are
  its data-access backends.
- **Chunking is fence-aware, not line-aware** (2026-09-06). `rag/chunker.py`
  threads a fence state machine through every structural split: a `#` line
  inside a ` ``` `/`~~~` fence is never a heading, in-fence content is
  byte-preserved (packing must not insert separators inside fences), and an
  oversized fence splits into pieces that each re-open and re-close their own
  fence with the original info string (repair markers may overshoot
  `max_size`). `chunk_markdown_structured` → `Chunk` (text + heading
  breadcrumb) is the indexing input — breadcrumbs are retrieval signal, never
  prepended to stored/embedded-alone text; `chunk_markdown` stays the
  text-only wrapper for `services/agents.py`. Never decide structure with a
  line-level test alone — `tests/test_chunker.py` pins the fence invariants.
- **Retrieval quality gates live in `rag/retriever.py` only** (2026-09-05).
  `Settings` thresholds, constructor-injected into `Retriever` from
  `_build_retriever` in `api/deps.py` (shared by search, chat, and writing
  wiring): `KB_SEARCH_BM25_MIN_COVERAGE` (default `"70%"` — term-coverage
  `minimum_should_match` on the `chunk_text` leaf AND the title/heading
  identity group via `bm25_chunk_query` (identity coverage added
  2026-09-10: a lone stopword title hit must not activate the BM25 leg);
  the LIVE BM25 noise guard since 2026-09-08, because BM25 score scale is
  query-dependent and no absolute floor generalizes; `""` omits the key),
  `KB_SEARCH_BM25_MIN_SCORE` (default `0.0`, RETIRED as a relevance gate
  — mechanism + ES-side `min_score` plumbing kept as an operator escape
  hatch only; rationale at the `core/config.py` site),
  `KB_SEARCH_VECTOR_MAX_DISTANCE` (default 0.45 cosine distance ceiling;
  `>= 2.0` disables), `KB_SEARCH_RRF_MIN_RELATIVE` (default 0.35 fraction
  of top fused score; `0.0` disables). Pipeline order: leg gates →
  `fuse_rrf` → relative floor (`apply_relative_score_floor`) → `[:limit]`;
  when nothing survives, return `items=[]` (empty over noise — never pad
  to `limit`). The vector gate is two-tier (2026-09-05): when the ceiling
  empties the leg ONLY, a head-rescue tier admits rows within
  `min(leg_min + SEARCH_VECTOR_RESCUE_MARGIN, SEARCH_VECTOR_RESCUE_MAX_DISTANCE)`
  (defaults 0.15 / 0.85; `<= 0` on either disables rescue) — short-keyword
  query embeddings sit systematically farther from long chunks than long
  questions (measured 2026-09-05: pure-CJK heads 0.48-0.61 vs long-query
  0.22; re-measured 2026-09-06 after the breadcrumb-enriched embedding input
  (`embedding_input`): pure-CJK heads 0.50-0.53, `redis` 0.46, long-query
  0.31 — same band, defaults still cover it), and
  the cap keeps rare-term legs silent. Since 2026-09-10 the rescue tier is
  additionally gated by an on-domain trigger:
  `KB_SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE` (default 0.62, calibrated
  on the real 15-doc corpus — see task `09-10-irrelevant-query-noise-gates`
  design.md § Calibration): rescue fires only when the emptied leg's own
  minimum distance is at or below it; an off-domain leg stays empty
  (empty beats noise); `>= 2.0` disables the trigger, restoring the
  pre-09-10 rescue-on-any-empty-primary behavior.
  `tests/test_vector_distance_probe.py`
  (`live_llm`, excluded from the default run) recalibrates the window and
  the trigger.
  `retrieve()` also truncates the query once at entry to
  `SEARCH_MAX_QUERY_LENGTH` (default 256, `<= 0` disables) — the single
  enforcement point for API and agent tools; standard-analyzer CJK yields
  ~1 token per char, so uncapped queries overflow Lucene's 1024 clause
  limit and 502 the ES leg. `SearchHit` carries optional `es_score` / `vector_distance`
  (None when that leg did not rank the chunk); `Retriever` leg gates are
  raw-leg quantities (vector distance ceiling, BM25 term coverage), never
  RRF rank-derived scores. Routers,
  services, and agents consume the gated retriever as-is — no re-filtering
  or re-thresholding outside `rag/`. Tests pinning legacy ungated behavior
  use `GATES_OFF` from `tests/fakes.py`; a drift-guard unit test keeps
  retriever constructor defaults in sync with `Settings`.
- **`mcp/` owns external tool integration**: server connections
  (stdio + HTTP transports), tool discovery/registry, and wrapping MCP tools
  so agents can register them like local functions. Tool results are passed
  through as structured data; no knowledge-base business logic lives here.
- **Routers** parse/validate input, call exactly one service method, map the
  result to a response schema (or an SSE stream). No business meaning.
- **Repositories** own every SQL query; `search/` owns every ES query.
  ES clients have two explicit lifecycles: the read path (retrieval) shares
  one process-lifetime client (`get_shared_es_client`, lru_cache in
  `search/es.py`), while the indexing pipeline builds its own short-lived
  client per run and closes it — never a client per request.
- **Framework callbacks are injected as plain callables.** Services stay
  framework-free by depending on a callable type they own (e.g.
  `ReindexEnqueuer = Callable[[UUID, datetime], None]` in
  `services/document.py` — id + the commit-time `updated_at` version
  stamp for the indexing generation guard); the FastAPI adapter bridging
  it to `BackgroundTasks` lives in `api/deps.py` — the **only** module
  allowed to import `BackgroundTasks`. Reference implementation:
  document create/update → `deps.py` enqueuer → `rag/indexer.run_indexing`.
- **Prompt templates** (`agents/prompts/`) are versioned assets — changing a
  prompt is a reviewable code change, not a runtime config tweak.
- **SSE endpoints streaming the chat event vocabulary** (writing, later
  siblings) reuse `to_sse`/`_EVENT_NAMES` from `api/v1/endpoints/chat.py` —
  it owns that wire format (payloads in `schemas/chat.py`). Hoist to a
  neutral `api/` helper when a third streamer appears.
- **Three agent shapes are now reference patterns**: QA (streaming,
  retrieval-first tool), summarize (sync plain text, map-reduce),
  association (sync structured output over pre-gathered deterministic
  candidates, joined back to candidate metadata), writing (streaming,
  retrieval-optional tool). Copy the closest one; the structured-output
  join-back pattern (LLM picks + deterministic metadata) is the
  hallucination guard — never return LLM-invented entities.

### Adding a new feature

Standard entity (CRUD):

1. Model in `models/<entity>.py` (+ Alembic migration).
2. Schemas in `schemas/<entity>.py` (`<Entity>Create` / `Update` / `Read`).
3. Repository in `repositories/<entity>.py` (`<Entity>Repository`).
4. Service in `services/<entity>.py` (`<Entity>Service`).
5. Router in `api/v1/endpoints/<entity_plural>.py`, registered in
   `api/v1/router.py`.

New agent capability (e.g. a new knowledge task):

1. Prompt template(s) in `agents/prompts/<task>.md`.
2. Agent definition in `agents/<task>.py` (Pydantic AI `Agent`, model from
   `llm/models.py`, tools from `rag/retriever.py` + `mcp/tools.py`).
3. Orchestrating service method in `services/chat.py` (or a dedicated
   service) — builds context, runs the agent, streams results.
4. Router/SSE endpoint if user-facing.
5. Tests with a faked model (see quality-guidelines.md).

---

## Naming Conventions

- **Files/directories**: `snake_case`, singular for entity modules
  (`document.py`), plural for endpoint modules (`documents.py`).
- **Classes**: `PascalCase` (`DocumentService`, `QAAgent`,
  `DocumentRepository`).
- **Pydantic schemas**: `<Entity>Create` / `<Entity>Update` / `<Entity>Read`.
- **Functions**: `snake_case`, verbs (`get_by_id`, `create`, `soft_delete`,
  `retrieve`, `embed_chunks`).
- **Constants + env vars**: `UPPER_SNAKE_CASE` (`DATABASE_URL`,
  `EMBEDDING_BASE_URL`, `EMBEDDING_DIM`).
- **Tests**: `test_<feature>_<behavior>.py::test_*`, e.g.
  `test_chat_api.py::test_qa_streams_citations`.

---

## Examples

The AI-stack portions of these specs were written before implementation;
code examples are canonical shapes. The first implemented vertical slice
(`documents` CRUD + `QAAgent` chat) becomes the reference implementation —
copy its shape when adding entities/agents, and update these examples to
match actual code once it exists.
