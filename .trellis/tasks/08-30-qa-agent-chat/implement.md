# Implement: QAAgent + SSE Chat

Ordered checklist. Gates after each group; single commit at the end
(no schema change).

## C1 — Model factory + prompt + agent

- [ ] Verify `pydantic_ai.models.openai` imports with current deps; if
      not, `uv add "pydantic-ai-slim[openai]"`
- [ ] `llm/models.py`: `get_chat_model(settings) -> Model`
- [ ] `agents/__init__.py`, `agents/prompts/qa.md` (grounded-answering
      contract), `agents/qa.py`: `ChatDeps`, `search_knowledge` tool,
      `build_qa_agent(model)`; prompt loaded at import; pyproject
      package-data for the .md if needed
- [ ] `core/exceptions.py`: `ChatUnavailableError` (503, `chat_unavailable`)
- [ ] Validation: `uv run mypy src` (pydantic-ai typing), ruff, format

## C2 — Service + endpoint

- [ ] `schemas/chat.py` (ChatRequest; ChatEvent variants)
- [ ] `services/chat.py`: `ChatService.ask` async generator — run_id
      contextvar, run_started/sources/answer_delta/done/error events,
      `agent_run_started`/`agent_run_finished` logs (question_length
      never the question), never raises after first event
- [ ] `api/deps.py`: `get_chat_service` (key check →
      ChatUnavailableError; retriever wiring mirrors search deps)
- [ ] `api/v1/endpoints/chat.py`: POST /chat SSE (EventSourceResponse);
      register router
- [ ] Validation: ruff + format + mypy; app boots, 422 paths behave

## C3 — Tests

- [ ] `tests/fakes.py`: FunctionModel scripting helper (tool call → text)
- [ ] `tests/test_chat_service.py` (offline): event order, sources,
      mid-stream failure ⇒ terminal error, run_id in logs
- [ ] `tests/test_chat_api.py` (offline, dep override): SSE contract,
      422s, 503 no-key envelope
- [ ] `tests/test_qa_agent.py` (db+es): real retriever tool against
      seeded corpus, sources reflect hits; optional `live_llm` smoke
- [ ] Validation: full `uv run pytest` (compose up); offline probe-run
      spot check green; full gates

## Review gates

- trellis-check dispatch after C3: five spec files, PRD acceptance sweep,
  layering (agents import no services; endpoint thin; SSE error rule),
  gates re-run.

## Rollback

- Single revert; no migration; retrieval layer untouched.
