# Directory Structure

> How backend code is organized in this project.

---

## Overview

- **Stack**: Python 3.12+ / FastAPI, fully async.
- **Layout**: `src/` layout with a single top-level package `app`.
- **Layering**: `router → service → repository` (strict, see below).
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
│   ├── repositories/         # data access layer (DB queries only)
│   │   └── document.py
│   ├── services/             # business logic
│   │   └── document.py
│   ├── api/
│   │   ├── deps.py           # shared FastAPI dependencies
│   │   └── v1/
│   │       ├── router.py     # aggregates endpoint routers for v1
│   │       └── endpoints/
│   │           └── documents.py
│   └── utils/                # pure helpers with no I/O
└── tests/
    ├── conftest.py           # async test fixtures, test DB, client factory
    ├── test_documents_api.py # endpoint tests (name: test_<feature>_<behavior>)
    └── test_documents_service.py
```

---

## Module Organization

### Layering rules (strict)

| Layer | Directory | May import | Must not import |
|-------|-----------|-----------|-----------------|
| Router | `api/` | `services/`, `schemas/`, `api/deps.py` | `models/`, `repositories/`, SQLAlchemy |
| Service | `services/` | `repositories/`, `models/`, `schemas/`, `core/` | `api/`, FastAPI objects (`Request`, `Response`) |
| Repository | `repositories/` | `models/`, `core/database.py` | `services/`, `api/`, Pydantic schemas |

Enforced conventions:

- **Routers** parse/validate input, call exactly one service method, and map
  its return value to a response schema. No `if` chains with business meaning,
  no direct DB access.
- **Services** own business rules, transactions, and cross-repository
  orchestration. They are framework-agnostic plain functions/classes —
  no `Request`/`Depends`/HTTP status codes here.
- **Repositories** own every SQL query. They take/return ORM models
  (or scalar values), never Pydantic schemas. A query that appears twice
  belongs in a repository method, not duplicated in a service.

### Adding a new feature

1. Model in `models/<entity>.py` (+ Alembic migration).
2. Schemas in `schemas/<entity>.py` (`<Entity>Create`, `<Entity>Update`,
   `<Entity>Read`).
3. Repository in `repositories/<entity>.py` (`DocumentRepository`).
4. Service in `services/<entity>.py` (`DocumentService`).
5. Router in `api/v1/endpoints/<entity_plural>.py`, registered in
   `api/v1/router.py`.

---

## Naming Conventions

- **Files/directories**: `snake_case`, singular for entity modules
  (`document.py`), plural for endpoint modules (`documents.py`).
- **Classes**: `PascalCase` (`DocumentService`, `DocumentRepository`).
- **Pydantic schemas**: `<Entity>Create` / `<Entity>Update` / `<Entity>Read`.
- **Functions**: `snake_case`, verbs (`get_by_id`, `create`, `soft_delete`).
- **Constants + env vars**: `UPPER_SNAKE_CASE` (`DATABASE_URL`, `SETTINGS`).
- **Tests**: `test_<feature>_<behavior>.py::test_*`, e.g.
  `test_documents_api.py::test_create_document_returns_201`.

---

## Examples

The project was scaffolded from empty; the first implemented entity
(`models/document.py` + its repository/service/router) is the reference
implementation — copy its shape when adding new entities.
