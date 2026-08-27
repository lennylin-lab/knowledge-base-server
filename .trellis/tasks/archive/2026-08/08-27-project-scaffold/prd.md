# Project Scaffold: FastAPI App + DB + Compose + Toolchain

## Goal

Stand up the runnable foundation of `knowledge-base-server` per the specs in
`.trellis/spec/backend/`: installable package, app factory, config/logging/
error plumbing, async DB with pgvector migration path, dev infrastructure
(PostgreSQL + Elasticsearch via compose), and the four quality gates
(ruff / mypy / pytest / uv) wired into `pyproject.toml` and green from day one.

This is **skeleton only** — no documents CRUD, no agents, no RAG, no MCP.
Those land as their own tasks.

## Requirements

1. `pyproject.toml` managed by uv; deps pinned in `uv.lock`; package
   installable as `app` from `src/` layout (PEP 621, hatchling).
2. `create_app()` factory in `src/app/main.py`; `/healthz` liveness endpoint
   at root scope returning `{"status": "ok"}`; v1 API router mounted at
   `/api/v1` (empty for now).
3. `core/config.py` — pydantic-settings `Settings` with: `DATABASE_URL`,
   `ELASTICSEARCH_URL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`,
   `EMBEDDING_MODEL`, `EMBEDDING_DIM=1536`, `CHAT_MODEL`, `LOG_LEVEL`,
   `LOG_FORMAT`. Env prefix `KB_`; `.env` loaded; `.env.example` committed.
4. `core/logging.py` — structlog configured per logging-guidelines.md
   (contextvars merge, JSON or console renderer by `LOG_FORMAT`); request-id
   middleware binding `request_id` + `X-Request-ID` response header.
5. `core/exceptions.py` — `AppError` hierarchy with shared exception
   handlers producing the standard error envelope; request-validation (422)
   errors reshaped to the same envelope.
6. `core/database.py` — async engine + `async_sessionmaker` + `get_db`
   dependency; declarative `Base`.
7. Alembic (async template) wired to `Settings.DATABASE_URL` and `Base`
   metadata; revision 0001 = `CREATE EXTENSION IF NOT EXISTS vector`.
8. `docker-compose.yml` — PostgreSQL 16 (pgvector image) + Elasticsearch 8
   single-node (security off, dev ports), healthchecks, named volumes.
9. Tests: `tests/conftest.py` + health-endpoint test using
   `httpx.AsyncClient` ASGI transport (no DB required for this task's tests).
10. All four gates green: `uv run ruff check .`, `uv run ruff format --check .`,
    `uv run mypy src`, `uv run pytest`.
11. App boots: `uv run uvicorn app.main:app` starts and `/healthz` answers.

## Out of Scope

- Documents/tags models, CRUD, Markdown parsing
- Agents, LLM provider factory, embeddings, RAG, MCP, ES indexing
- Auth, CORS setup for a real frontend origin, deployment manifests
- CI pipeline files (separate task when a remote exists)

## Acceptance Criteria

- [x] `uv sync` succeeds from clean checkout (lockfile committed)
- [x] `uv run pytest` green with ≥1 health test; no network calls
- [x] `uv run ruff check . && uv run ruff format --check . && uv run mypy src` clean
- [x] `docker compose up -d` starts PG (vector extension available) + ES; both healthy
- [x] `uv run alembic upgrade head` applies 0001 against compose PG
- [x] `/healthz` returns 200 `{"status": "ok"}` via local uvicorn boot
- [x] Unknown route returns the standard error envelope (404, code `not_found`)
