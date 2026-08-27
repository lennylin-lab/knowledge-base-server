# Backend Development Guidelines

> Coding conventions for `knowledge-base-server` (backend-only workspace).

---

## Overview

- **Stack**: Python 3.12+ / FastAPI (fully async) + PostgreSQL + SQLAlchemy
  2.0 async + Alembic.
- **Toolchain**: uv + ruff + mypy + pytest.
- **Layering**: `router → service → repository`, strictly enforced.
- Frontend lives in a separate repository; this workspace has no frontend
  spec on purpose.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | src layout, layering rules, naming conventions | Filled |
| [Database Guidelines](./database-guidelines.md) | SQLAlchemy 2.0 async, repositories, Alembic migrations | Filled |
| [Error Handling](./error-handling.md) | `AppError` hierarchy, response envelope, per-layer rules | Filled |
| [Logging Guidelines](./logging-guidelines.md) | structlog structured events, request context, levels | Filled |
| [Quality Guidelines](./quality-guidelines.md) | toolchain gates, testing requirements, review checklist, forbidden patterns | Filled |

---

## Conventions established at bootstrap (2026-08-27)

Decisions made before the first line of code — treat them as binding:

1. FastAPI app factory pattern (`create_app()` in `main.py`).
2. All DB access via repositories using async `select()`; `session.query()`
   is forbidden.
3. Domain errors subclass `AppError`; one shared error response envelope.
4. structlog with request-id contextvars middleware.
5. `uv run <cmd>` is the canonical way to run anything; config in
   `pyproject.toml`.

Code examples in these files are canonical shapes written at bootstrap;
once real modules exist, prefer referencing them and update the examples
to match actual code.

---

**Language**: All documentation is written in **English**.
