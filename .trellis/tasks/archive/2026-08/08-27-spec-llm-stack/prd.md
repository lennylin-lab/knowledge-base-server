# Refine Backend Specs for LLM Agent Knowledge Base Stack

## Background

Product direction confirmed: an LLM-Agent-based intelligent knowledge base
system — Markdown-first knowledge management (tags, full-text search) with
built-in agents (Q&A, summarization, knowledge association, writing assist),
MCP tool extension (Context7, Web Search), RAG, multi-source knowledge
fusion, and OpenAI-API-compatible LLM adapters.

The bootstrap specs cover the generic FastAPI layer only. This task extends
`.trellis/spec/backend/` to cover the AI/agent stack decisions so future
implement tasks generate code that matches them.

## Decisions (binding, confirmed by lenny on 2026-08-27)

| Slot | Choice |
|------|--------|
| Agent framework | Pydantic AI |
| LLM access | `openai` SDK with configurable `base_url` (OpenAI-compatible) |
| Embeddings | OpenAI-compatible embedding API, 1536-dim, provider-abstracted |
| Vector store | PostgreSQL pgvector (HNSW) |
| Full-text search | Elasticsearch (BM25) |
| Hybrid retrieval | ES BM25 + pgvector cosine, RRF fusion |
| MCP | official `mcp` Python SDK (client; stdio + HTTP) |
| Markdown | markdown-it-py + python-frontmatter |
| Streaming | sse-starlette |
| Background jobs | FastAPI BackgroundTasks now; ARQ + Redis when volume demands |
| Users | single-user MVP; ownership columns reserved for multi-user upgrade |

## Scope

Update only, no code:

- [x] `directory-structure.md` — new modules (`agents/`, `llm/`, `rag/`,
      `mcp/`, `search/`), extended layering matrix incl. agent layer
- [x] `database-guidelines.md` — pgvector column/index/query patterns,
      hybrid retrieval, chunk/embedding tables
- [x] `error-handling.md` — LLM/provider/MCP/streaming error taxonomy
- [x] `logging-guidelines.md` — LLM call/agent run/MCP tool logging rules
- [x] `quality-guidelines.md` — LLM-dependent test strategy (no live calls),
      new forbidden patterns
- [x] `index.md` — stack overview refreshed

## Acceptance Criteria

1. Every new module directory has a stated purpose and layering rule
   (may/must-not import).
2. pgvector usage documented with canonical model + query examples, including
   the fixed 1536-dim decision and its migration implication.
3. Error taxonomy maps every LLM/MCP failure mode to a status code and
   envelope code.
4. Tests must be runnable offline; live LLM tests are opt-in.
5. Specs document reality-vs-plan honestly: mark AI-stack examples as
   canonical shapes until real code lands.

## Out of Scope

- Any scaffolding/implementation of `src/`
- Frontend (separate repo)
- Choosing embedding vendor beyond "OpenAI-compatible endpoint" (config)
