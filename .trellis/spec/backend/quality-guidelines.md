# Quality Guidelines

> Code standards, review checklist, testing requirements, forbidden patterns.

---

## Toolchain

Managed by **uv**; all config lives in `pyproject.toml` (no separate
setup.cfg/.flake8).

| Tool | Role | Command |
|------|------|---------|
| uv | deps + venv + running | `uv sync`, `uv run <cmd>` |
| ruff | lint **and** formatter (replaces black/isort) | `uv run ruff check .`, `uv run ruff format .` |
| mypy | static types, strict on `src/` | `uv run mypy src` |
| pytest | tests | `uv run pytest` |

**All four must pass before any commit.** CI runs exactly these commands —
keep them fast enough to run locally on every change.

## Python Style

- Target Python 3.12+; modern syntax (`X | None`, `match`, f-strings).
- Every function in `src/` has type hints; services/repositories use
  domain types (`UUID`, ORM models), not primitives, where meaningful.
- `from __future__ import annotations` at the top of new modules.
- Public module functions > classes unless state is real
  (repositories carry a session; services may be plain functions).
- Docstrings: one-liners for simple functions; full docstrings for service
  methods and anything non-obvious. Comments explain *why*, not *what*.

## Testing Requirements

- **Framework**: pytest + `pytest-asyncio` + `httpx.AsyncClient` against the
  app (ASGI transport, no live server).
- **Layers tested**: endpoints (API contract + status codes), services
  (business rules, domain exceptions). Repositories are covered via service
  tests against the test DB.
- **DB**: tests run against a disposable PostgreSQL database (e.g. testcontainers
  or a dedicated local `kb_test` DB), schema created per-session via
  `Base.metadata.create_all`, truncated between tests in fixtures —
  never the dev database.
- **Naming**: `test_<unit>_<behavior>`, e.g. `test_get_document_missing_raises_404`.
- Arrange–Act–Assert, one behavior per test; shared setup in `conftest.py`
  fixtures, not copy-pasted blocks.

```python
# tests/test_documents_api.py (canonical shape)
async def test_create_document_returns_201(client, db):
    resp = await client.post("/api/v1/documents", json={"title": "Notes"})

    assert resp.status_code == 201
    assert resp.json()["title"] == "Notes"
```

New features ship with tests in the same change; bug fixes ship with a test
that reproduces the bug first.

### AI-stack test strategy (offline by default)

- **No live LLM calls in the default test run.** Agents are tested with
  Pydantic AI's `TestModel` (canned tool-call sequences) or `FunctionModel`
  (scripted outputs). Provider SDK behavior (retries, error mapping) is
  tested by mocking the `openai` client at the `llm/` boundary only —
  nothing below `llm/` may import provider SDKs, so this boundary is stable.
- Tests that must hit a real provider are marked `@pytest.mark.live_llm`
  and **excluded by default** (`-m "not live_llm"` in pyproject); they are
  for manual/nightly verification.
- **Elasticsearch**: query builders are pure functions tested without a
  server; client integration tests use testcontainers and are skippable
  when Docker is unavailable (`ES_INTEGRATION=1` gate).
- **pgvector**: test database image must include the `vector` extension;
  similarity tests assert ordering, not exact distances.
- **SSE endpoints**: tests consume the stream via `httpx.AsyncClient` and
  assert event sequence, including the terminal `error` event path.
- **RRF fusion and chunking** are pure logic — exhaustive unit tests, no
  infrastructure.

## Review Checklist (apply before requesting/merging)

1. `uv run ruff check . && uv run ruff format --check .` clean.
2. `uv run mypy src` clean.
3. `uv run pytest` green, new code covered, no live LLM calls.
4. Layering respected — routers thin, services framework-free, repositories
   SQL-only, agents free of `services/`/FastAPI imports (see
   directory-structure.md).
5. Errors raised as `AppError` subclasses with the right status/code;
   SSE failures emit the terminal `error` event.
6. New queries: no N+1, eager loads where attributes are accessed.
7. Schema changes: Alembic revision with working downgrade; embedding
   dimension untouched.
8. No secrets or tokens in code, logs, or test fixtures (use env/Settings).
9. Model names, `base_url`, timeouts, retry counts come from `Settings` —
   no literals in `src/`.
10. Prompt changes edit files under `agents/prompts/` (reviewable diff),
    not inline f-strings buried in Python.

## Forbidden Patterns

- `print()` in `src/` — use structlog.
- `# type: ignore` without an explanatory comment.
- `except Exception: pass` — see error-handling.md.
- Bare `Any` in public signatures.
- Relative imports beyond the module's package (`from . import x` ok;
  `from ...core import y` not).
- Direct `requests`/blocking I/O in async paths — use `httpx.AsyncClient` /
  async drivers.
- Hardcoded config (URLs, ports, paths) — everything flows through
  `core/config.py` Settings.
- Instantiating `openai.AsyncOpenAI` or Pydantic AI models outside `llm/` —
  one factory, one place for retries/timeouts.
- Synchronous LLM/embedding calls (`client.embeddings.create` without await)
  or LLM calls on the event loop that could block — provider I/O is always
  async.
- Live provider calls in unmarked tests (must be `live_llm`).
- Secrets in prompts, logs, or ES documents — indexing strips front-matter
  secrets before persistence.
