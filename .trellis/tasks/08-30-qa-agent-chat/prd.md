# QAAgent with SSE Streaming Chat Endpoint

## Goal

The product's core interaction: ask a question in natural language, get a
streamed answer grounded in the knowledge base, with citations to the
source documents. Wires the existing retrieval layer into a Pydantic AI
agent and exposes it as `POST /api/v1/chat` (SSE).

Single-turn stateless (user decision): each request = question →
retrieval → streamed answer. No conversation history, no session
persistence — that is a later task.

## Requirements

1. **Model factory** (`llm/models.py`): build the Pydantic AI chat model
   from Settings (`CHAT_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`).
   `llm/` stays a pure provider layer — no domain knowledge. Verify the
   `pydantic-ai-slim` openai extra is importable (add the extra to the
   dependency if the bare slim package lacks it).
2. **Prompt asset** (`agents/prompts/qa.md`): versioned markdown template.
   Rules baked in: answer ONLY from retrieved context; cite sources by
   the bracketed numbers given in tool results; if the context is
   insufficient, say so plainly instead of guessing; reply in the
   question's language; markdown formatting allowed.
3. **QAAgent** (`agents/qa.py`): Pydantic AI `Agent` with the model from
   `llm/models.py` and a `search_knowledge` tool backed by
   `rag/retriever.py`. Per-request state flows through the framework's
   deps mechanism (`RunContext[ChatDeps]`), never through module/global
   state — the agent object itself may be reused across requests.
   `agents/` imports no services (spec layering).
4. **Chat service** (`services/chat.py`): binds `run_id` (uuid4) via
   contextvars, runs the agent, yields typed stream events
   (`run_started`, `sources`, `answer_delta`, `done`, terminal `error`).
   Provider failure mid-stream ⇒ terminal `error` event, stream closed,
   never left hanging (error-handling spec). Missing API key ⇒ fail
   fast with a clear error before the stream starts (chat cannot
   degrade — unlike search there is no non-LLM fallback).
5. **Endpoint** (`api/v1/endpoints/chat.py`): `POST /api/v1/chat`,
   request `{question: str (min 1), limit?: int (1..20, default 8)}`,
   response `text/event-stream` via sse-starlette. Router thin: parse,
   one service call, stream mapped. Registered in `api/v1/router.py`.
6. **Retrieval citations**: the `sources` event carries the retrieval
   results (document_id, document_title, chunk_index, score) collected
   during the run; the prompt ties bracketed citations in the answer to
   those sources. `search_executed`/retrieval logging already exists —
   the chat path reuses the retriever, no duplicate logging.
7. **Tests**: agent/service with a faked model (pydantic-ai
   `FunctionModel`) scripting tool calls and streamed text — offline for
   pure stream/error semantics; one db+es integration test (seeded
   corpus, FunctionModel invoking the real retrieval tool); SSE endpoint
   contract test (event order/types, 422 on empty question, error event
   on provider failure); optional real-provider smoke under `live_llm`.

## Out of Scope

- Multi-turn conversations, session persistence, history compression
  (later task; `chat_sessions`/`chat_messages` schema deliberately absent)
- The other three agents (summarize / association / writing)
- MCP tools (mcp/ does not exist yet; the agent's tool set is retrieval
  only this task)
- Streaming usage/token accounting UI, chat persistence/audit log
- Auth/ownership (single-user MVP as before)

## Acceptance Criteria

- [ ] `POST /api/v1/chat` with a seeded corpus and a scripted model that
      calls the retrieval tool: SSE stream observed with `run_started` →
      `sources` (real retrieval results from the seeded corpus) → one or
      more `answer_delta` → `done` (tested db+es)
- [ ] Question about content only present in an indexed document is
      answered with that document cited in `sources` (integration test)
- [ ] Provider failure mid-stream ⇒ terminal `error` SSE event with the
      envelope code/message, stream then closes (tested with faked model)
- [ ] Missing API key ⇒ request fails before streaming with a clear
      error (no hang, no empty 200 stream)
- [ ] Empty/missing question ⇒ 422 envelope; `limit=0`/`21` ⇒ 422
- [ ] `run_id` present in `run_started`/`done` events and in every log
      line of the run (contextvar binding verified in a test)
- [ ] No agent/service test hits a real LLM (FunctionModel only); a
      live smoke exists only under `live_llm` (deselected by default)
- [ ] Offline (compose down): `uv run pytest` green with visible skips
- [ ] Gates green: ruff check / ruff format --check / mypy src / pytest
