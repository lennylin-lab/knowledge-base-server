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
| RAG | `rag/` | `models/`, `repositories/`, `search/`, `llm/`, `core/` | `api/`, `services/`, `agents/` |
| MCP | `mcp/` | `core/`, `schemas/`, `pydantic-ai` (tool wrapping in `mcp/tools.py` only) | `api/`, `services/`, `agents/`, `models/`, FastAPI |
| LLM | `llm/` | `core/` | everything domain (`services/`, `agents/`, `rag/`, …) |
| Repository | `repositories/` | `models/`, `core/database.py` | `services/`, `api/`, `agents/`, Pydantic schemas |

Key points:

- **Services drive agents, never the reverse.** A service picks the agent,
  builds its run context, and registers extra tools as closures — that is how
  service capabilities reach an agent without `agents/` importing `services/`
  (prevents import cycles).
- **`llm/` is a pure provider layer**: model instances, embedding clients,
  retries/timeouts. It knows nothing about documents or knowledge. All model
  names, `base_url`, API keys come from `Settings` — nothing hardcoded.
- **`rag/` owns the retrieval pipeline**: chunking, embedding, hybrid search
  (ES BM25 + pgvector cosine), RRF fusion. `search/` and `repositories/` are
  its data-access backends.
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
  `ReindexEnqueuer = Callable[[UUID], None]` in `services/document.py`);
  the FastAPI adapter bridging it to `BackgroundTasks` lives in
  `api/deps.py` — the **only** module allowed to import `BackgroundTasks`.
  Reference implementation: document create/update → `deps.py` enqueuer →
  `rag/indexer.run_indexing`.
- **Prompt templates** (`agents/prompts/`) are versioned assets — changing a
  prompt is a reviewable code change, not a runtime config tweak.

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
