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

### Tooling gotchas (learned 2026-08-27, scaffold task)

> **Warning**: ruff config MUST keep the extend-exclude for Trellis harness
> directories (`.trellis/`, `.claude/`, `.codex/`, `.cursor/`, `.agents/`).
> These hold tooling scaffolds, not project code. Incident: a repo-wide
> `ruff format .` rewrote Trellis hook scripts and had to be reverted with
> `git restore`. If the excludes are ever dropped, `ruff format` will
> silently reformat harness files again.

- `types-python-frontmatter` does not exist on PyPI. When the Markdown
  pipeline (`rag/`) starts importing `python-frontmatter`, add a mypy
  override (`[[tool.mypy.overrides]] module = "frontmatter" ignore_missing_imports = true`)
  instead of hunting for stubs.
- Task artifacts: `check.jsonl` for code-touching tasks lists **all five**
  backend spec files — a manifest that omits one (e.g. database-guidelines)
  under-informs the check agent even when the dispatch prompt requires it.

### Tooling gotchas (learned 2026-08-30, MCP task)

- **Dependency floors must match the API actually imported.** Incident:
  `mcp>=1.0` in pyproject while `mcp/` imports the 2.x high-level
  `Client` — a fresh resolution to 1.x would break every import. When a
  task starts using a new major-version API of an existing dep, raise the
  floor in the same change and re-lock.
- **`.env.example` must enumerate every Settings group.** Adding a
  Settings field without its commented example entry makes the config
  surface undiscoverable; when a new group lands (e.g. `KB_MCP_CONFIG_PATH`),
  update `.env.example` in the same task.
- **Helpers shared across test modules live in `tests/fakes.py` (doubles,
  parsers) or `tests/corpus.py` (fixtures/data) — never in a `test_*`
  module.** Test-module → test-module imports are the one edge the suite
  must not grow (parse_sse was relocated for exactly this).

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
  for manual/nightly verification. Same rule for real infrastructure:
  `live_mcp` (real MCP server) and `live_redis` (real queue round trip,
  probe-skip when unreachable) follow the identical pattern — the default
  suite constructs zero live connections to providers, MCP servers, or
  Redis.
- **Elasticsearch**: query builders are pure functions tested without a
  server; client integration tests use testcontainers and are skippable
  when Docker is unavailable (`ES_INTEGRATION=1` gate).
- **pgvector**: test database image must include the `vector` extension;
  similarity tests assert ordering, not exact distances.
- **SSE endpoints**: tests consume the stream via `httpx.AsyncClient` and
  assert event sequence, including the terminal `error` event path.
- **RRF fusion and chunking** are pure logic — exhaustive unit tests, no
  infrastructure.

> **Warning (learned 2026-09-10, query-rewrite task): FunctionModel fakes
> that run with `message_history` must record the LAST user message as the
> run's own prompt, and prompt-wiring needs its own assertion.** Two related
> traps: (1) with history present, the run's own prompt is the last user
> entry — a first-user-message recorder captures the oldest prior turn and
> makes every history-bearing assertion wrong
> (`tests/fakes.py::_last_user_prompt` is the pattern). (2) A tool-scripted
> QA fake emits its tool calls unconditionally, so downstream assertions
> (`StubRetriever.calls`) stay green even if the service passes the RAW
> question to `run_stream` instead of the transformed one — the central
> wiring is vacuously passing. Pin the wiring by asserting the recorded run
> prompt itself (e.g. `_user_prompts(histories[0]) == [original,
> transformed]`), which simultaneously re-pins what the history carried.

> **Warning (learned 2026-09-05, content-hash task): the default suite can
> silently require Redis via the dev `.env`.** The "zero live connections"
> rule above has one hole: when `.env` sets `KB_REDIS_URL`,
> `make_index_enqueuer` (`api/deps.py`) wires the ARQ transport app-wide, so
> the `@pytest.mark.es` e2e indexer tests (`tests/test_indexer.py`) enqueue
> through ARQ and fail on their status assertions when Redis is down —
> indistinguishable from a product bug. Verdict procedure for "is this
> failure mine?": run the failing tests in a HEAD worktree
> (`git worktree add /tmp/x HEAD`) with the same env — an identical failure
> there means pre-existing/environmental; never `git stash` on a moving
> tree. Fixed (2026-09-05): the `indexing_client` fixture pins the
> BackgroundTasks transport by patching `app.api.deps.get_settings` to an
> empty-`REDIS_URL` settings copy, so e2e tests never depend on the queue
> transport. Any future fixture that exercises the real deps write path
> needs the same pin.

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
9. Model names and `base_url` come from `Settings` — no literals in
   `src/`. SDK timeout/retry defaults are the one exception: they live as
   provider-layer constants in `llm/` (`_REQUEST_TIMEOUT`,
   `_MAX_RETRIES` — same defaults across embeddings and chat).
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
