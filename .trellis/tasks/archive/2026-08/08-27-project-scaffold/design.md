# Design: Project Scaffold

## Package & Interpreter

- PEP 621 `pyproject.toml`, build backend **hatchling**, `src/` layout,
  package name `app` (per directory-structure.md).
- `.python-version` pinned to **3.12** (`requires-python = ">=3.12"`);
  uv resolves that interpreter. Rationale: spec floor; wheels for every dep
  (pgvector, pydantic-ai, mcp) are guaranteed on 3.12; 3.13/3.14 can be
  raised later deliberately.
- Dev deps as uv dependency-groups (`[dependency-groups] dev = [...]`), not
  extras — uv installs them by default with `uv sync`.

## Dependencies (runtime)

| Dep | Purpose |
|-----|---------|
| `fastapi` | web framework |
| `uvicorn[standard]` | ASGI server (standard extras for uvloop/httptools) |
| `sqlalchemy[asyncio]` ≥ 2.0 | ORM, async engine |
| `asyncpg` | PG driver |
| `alembic` | migrations |
| `pgvector` | `Vector` column type for SQLAlchemy |
| `pydantic-settings` | `Settings` |
| `structlog` | logging |
| `sse-starlette` | SSE (registered now; used by chat endpoints later) |
| `openai` | OpenAI-compatible client (used later by `llm/`) |
| `pydantic-ai` (slim) | agent framework (used later by `agents/`) |
| `mcp` | MCP client SDK (used later by `mcp/`) |
| `markdown-it-py`, `python-frontmatter` | Markdown pipeline (later `rag/`) |
| `httpx` | async HTTP client (ES transport via `AsyncOpenAI`-style REST later; also tests) |
| `elasticsearch[async]` | official async ES client for `search/` |

Rationale for registering AI deps now: stack is decided (spec index);
locking versions once avoids churn in every later task. Unused-import lint
is not an issue since deps ≠ imports.

Dev group: `pytest`, `pytest-asyncio`, `ruff`, `mypy`,
`types-python-frontmatter` (mypy needs it; add if stubs missing).

## Module shapes

### `src/app/main.py`

```python
def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    app = FastAPI(title="knowledge-base-server", version="0.1.0")
    app.add_middleware(RequestIdMiddleware)  # core/logging.py
    register_exception_handlers(app)  # core/exceptions.py
    app.include_router(api_v1_router, prefix="/api/v1")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- `get_settings()` — module-level cached accessor (`@lru_cache`), so tests
  can `get_settings.cache_clear()` and monkeypatch env.

### `src/app/core/config.py`

- `Settings(BaseSettings)` with `model_config = SettingsConfigDict(
  env_prefix="KB_", env_file=".env", extra="ignore")`.
- Fields per prd.md #3. `DATABASE_URL` example:
  `postgresql+asyncpg://kb:kb@localhost:5432/kb`.
- Secrets typed `SecretStr` (`OPENAI_API_KEY`).

### `src/app/core/logging.py`

- `configure_logging(level, format)` per logging-guidelines.md canonical
  shape; `RequestIdMiddleware` (BaseHTTPMiddleware or pure ASGI — choose
  pure ASGI middleware for performance; uuid4 → contextvars +
  `X-Request-ID` header). Access log line at `info`: `http_request`
  (method, path, status, duration_ms).

### `src/app/core/exceptions.py`

- Hierarchy per error-handling.md: `AppError` (+ `NotFoundError`,
  `ConflictError`, `ValidationError`, `ForbiddenError`, `LLMProviderError`,
  `LLMRateLimitedError`, `MCPToolError`).
- `register_exception_handlers(app)` installs: `AppError` handler (envelope,
  `warning` log), `RequestValidationError` → 422 envelope, catch-all
  `Exception` → 500 generic envelope + `error` log with traceback, and
  `StarletteHTTPException` (404/405 from router) → envelope with
  code `not_found` / `method_not_allowed`.

### `src/app/core/database.py`

- `engine = create_async_engine(...)`, `SessionFactory =
  async_sessionmaker(engine, expire_on_commit=False)`, `get_db` async
  dependency, `class Base(DeclarativeBase)` with naming-convention
  metadata (ix_/uq_/fk_/pk_ prefixes) so Alembic autogen produces stable
  names.
- Engine is created lazily via function `build_engine(settings)` —
  import-time side effects (connecting config) kept out of module import
  so tests/mypy can import freely. Module holds a default instance built
  from `get_settings()`.

## Alembic (async)

- `alembic init -t async alembic`.
- `env.py`: read URL from `Settings` (`KB_DATABASE_URL`), set
  `target_metadata = Base.metadata`, import `app.core.database` so models
  register (no models yet — safe).
- `alembic.ini`: minimal; URL line left blank (env.py is source of truth).
- Revision `0001_enable_vector`: `op.execute("CREATE EXTENSION IF NOT EXISTS
  vector")`, downgrade `DROP EXTENSION IF EXISTS vector` (safe on fresh DB;
  destructive only when vector data exists — acceptable at bootstrap).

## docker-compose.yml

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment: POSTGRES_USER: kb, POSTGRES_PASSWORD: kb, POSTGRES_DB: kb
    ports: 5432:5432
    volumes: pgdata:/var/lib/postgresql/data
    healthcheck: pg_isready -U kb
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.17.0
    environment: discovery.type=single-node, xpack.security.enabled=false,
      ES_JAVA_OPTS=-Xms512m -Xmx512m
    ports: 9200:9200
    volumes: esdata:/usr/share/elasticsearch/data
    healthcheck: curl -f localhost:9200/_cluster/health
volumes: pgdata, esdata
```

Notes: ES 8.17 single-node, security off (dev only); requires host
`vm.max_map_count ≥ 262144` — documented in README with the sysctl command.

## Tests

- `tests/conftest.py`: `anyio_backend`/`pytest-asyncio` mode auto fixture,
  `client` fixture = `httpx.AsyncClient(transport=ASGITransport(app=create_app()))`.
- `tests/test_health_api.py`: `/healthz` 200 + shape; unknown route → 404
  envelope `error.code == "not_found"`.
- `pyproject` pytest config: `asyncio_mode = "auto"`, testpaths `tests`,
  marker `live_llm` registered, addopts `-m "not live_llm"`.
- No DB in this task's tests (no models yet); DB-dependent tests arrive with
  the documents task (conftest extension then).

## Tooling config (all in pyproject.toml)

- ruff: target py312, line-length 100; lint `E,F,I,UP,B,SIM,RUF` (+ `T` for
  tests? no), format defaults.
- mypy: `strict` on `src/`, `python_version = "3.12"`, plugins none yet.
- pytest: as above; coverage not enforced at scaffold.

## Tradeoffs / Rejected

- **Import-time engine** (rejected): breaks testability; lazy `build_engine`.
- **Sync SQLAlchemy first** (rejected): spec mandates async from day one;
  retrofit pain is real.
- **ES 7.x** (rejected): 8.x is current; security-off single node keeps dev
  simple.
- **Putting deps in extras** (rejected): uv groups are the modern default;
  one lockfile covers dev.

## Rollback

Single commit; `git revert` restores pre-scaffold state. Compose volumes
(`pgdata`, `esdata`) are dev-only and disposable.
