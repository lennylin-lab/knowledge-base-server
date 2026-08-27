# Implement: Project Scaffold

Ordered checklist. Run gates after each numbered group; commit after G2 and
G3 (rollback points).

## G1 — Project metadata & deps

- [ ] `pyproject.toml`: PEP 621 + hatchling, `requires-python >= 3.12`,
      ruff/mypy/pytest config per design.md, dependency-groups `dev`
- [ ] `.python-version` → 3.12
- [ ] `uv sync` (creates venv + lockfile)
- Validation: `uv sync` exits 0; `uv run python -c "import app"` fails only
  after src exists (expected at this stage)

## G2 — Core modules + app factory

- [ ] `src/app/__init__.py`
- [ ] `src/app/core/__init__.py`
- [ ] `src/app/core/config.py` — Settings per prd #3 (cached `get_settings`)
- [ ] `src/app/core/logging.py` — configure_logging + RequestIdMiddleware
- [ ] `src/app/core/exceptions.py` — AppError hierarchy + handlers
- [ ] `src/app/core/database.py` — Base (naming conventions), build_engine,
      SessionFactory, get_db
- [ ] `src/app/api/__init__.py`, `src/app/api/deps.py` (empty placeholder)
- [ ] `src/app/api/v1/__init__.py`, `src/app/api/v1/router.py` (empty router)
- [ ] `src/app/main.py` — create_app + `/healthz`
- [ ] `.env.example` with all `KB_*` vars
- Validation: `uv run ruff check . && uv run ruff format .`
              `uv run mypy src`

**Commit point G2** — "feat: app skeleton (factory, config, logging, errors)"

## G3 — Infrastructure: compose + alembic + tests

- [ ] `docker-compose.yml` per design.md
- [ ] `README.md` — quickstart: compose up, alembic upgrade, uvicorn run,
      sysctl note for ES
- [ ] `alembic init -t async alembic`; rewire `env.py` to Settings + Base
- [ ] revision 0001 enable pgvector (+ working downgrade)
- [ ] `tests/conftest.py` + `tests/test_health_api.py`
- Validation:
  - `uv run pytest` (green, no network)
  - `uv run ruff check . && uv run ruff format --check . && uv run mypy src`
  - `docker compose up -d && docker compose ps` (both healthy)
  - `uv run alembic upgrade head` against compose PG
  - `uv run uvicorn app.main:app --port 8000` + `curl localhost:8000/healthz`
    + `curl -i localhost:8000/nope` (envelope check, X-Request-ID header)
  - `docker compose down` (leave volumes)

**Commit point G3** — "feat: compose infra, alembic, health tests"

## Review gates

- trellis-check dispatch after G3 covering: spec compliance (all 5 backend
  spec files), PRD acceptance criteria sweep, gates green.
- Manual: confirm `.venv/`, `.env` untracked; `uv.lock` tracked.

## Rollback

- After G2: `git revert <g2-sha>`
- After G3: `git revert <g3-sha>`; `docker compose down -v` if wiping data
