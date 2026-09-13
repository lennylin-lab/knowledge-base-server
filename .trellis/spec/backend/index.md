# Backend Development Guidelines

> Coding conventions for `knowledge-base-server` (backend-only workspace).

---

## Overview

**Product**: LLM-Agent-based intelligent knowledge base — Markdown knowledge
management (tags, full-text search) with built-in agents (Q&A, summarization,
knowledge association, writing assist), MCP tool extension (Context7,
Web Search), RAG, hybrid retrieval, and OpenAI-compatible model adapters.

| Layer | Choice |
|-------|--------|
| Language / framework | Python 3.12+, FastAPI (fully async) |
| Data | PostgreSQL 16+ (SQLAlchemy 2.0 async, Alembic) + pgvector |
| Full-text search | Elasticsearch (BM25) |
| Hybrid retrieval | ES BM25 + pgvector cosine, RRF fusion in `rag/` |
| Agents | Pydantic AI |
| LLM access | `openai` SDK, `base_url` configurable (OpenAI-compatible) |
| Embeddings | OpenAI-compatible endpoint, 1536-dim, provider-abstracted |
| MCP | official `mcp` Python SDK (client; stdio + HTTP) |
| Markdown | markdown-it-py + python-frontmatter |
| Streaming | sse-starlette |
| Background jobs | BackgroundTasks now; ARQ + Redis when volume demands |
| Auth | single-user MVP; `owner_id` columns reserved |
| Toolchain | uv + ruff + mypy + pytest; structlog for logs |
| Layering | `router → service → agent → (rag | mcp | repository)` |

Frontend lives in a separate repository; this workspace has no frontend
spec on purpose.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | src layout, module map incl. agents/llm/rag/mcp/search, layering matrix | Filled |
| [Database Guidelines](./database-guidelines.md) | SQLAlchemy 2.0 async, pgvector patterns, Alembic migrations | Filled |
| [Error Handling](./error-handling.md) | `AppError` hierarchy + LLM/MCP/streaming taxonomy, response envelope | Filled |
| [Logging Guidelines](./logging-guidelines.md) | structlog events, request/run context, AI-stack logging rules | Filled |
| [Search Guidelines](./search-guidelines.md) | ES image + analysis-ik plugin build, mapping analyzers, index lifecycle; retrieval-quality contracts (BM25 shape, gates, vector rescue) + chat query rewrite | Filled |
| [Chat Guidelines](./chat-guidelines.md) | conversation-history contracts: token budget, long-turn guardrail, rolling summary, offline token counter; SSE progress-event ordering contracts | Filled |
| [Quality Guidelines](./quality-guidelines.md) | toolchain gates, offline AI test strategy, review checklist, forbidden patterns | Filled |
| [Cache Guidelines](./cache-guidelines.md) | opt-in Redis cache: Cache/NullCache/RedisCache primitive, key shapes, best-effort degradation, epoch invalidation | Filled |

---

## Conventions established at bootstrap (2026-08-27)

Decisions made before the first line of code — treat them as binding:

1. FastAPI app factory pattern (`create_app()` in `main.py`).
2. All PG access via repositories using async `select()`; `session.query()`
   is forbidden. ES access via `search/` only.
3. Domain errors subclass `AppError`; one shared error response envelope;
   SSE streams fail via a terminal `error` event.
4. structlog with request-id + agent-run-id contextvars middleware.
5. `uv run <cmd>` is the canonical way to run anything; config in
   `pyproject.toml`; all provider/model settings flow through `Settings`.
6. Agents never import services — dependencies are injected as tools by the
   orchestrating service (prevents cycles).
7. `llm/` is the only layer touching provider SDKs; everything below it is
   provider-agnostic and testable offline.
8. Prompts are versioned files under `agents/prompts/`, not inline strings.

Code examples in these files are canonical shapes written at bootstrap;
once real modules exist, prefer referencing them and update the examples
to match actual code.

---

**Language**: All documentation is written in **English**.
