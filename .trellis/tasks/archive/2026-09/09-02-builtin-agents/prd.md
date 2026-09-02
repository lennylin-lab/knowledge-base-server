# Built-in Agents: Summarize, Association, Writing (Parent)

## Goal

Complete the built-in agent suite on the infrastructure shipped with the
QAAgent slice: `summarize` (document summary), `association` (related
knowledge analysis), `writing` (assisted authoring). Each is its own
child task with an independent endpoint; the parent owns the shared
shape decisions and cross-child acceptance.

## Shared shape decisions (binding for all children)

- **Independent endpoints** (user decision):
  - `POST /api/v1/documents/{id}/summary` — summarize; synchronous
    JSON response
  - `POST /api/v1/documents/{id}/associations` — association; JSON
  - `POST /api/v1/writing/suggest` — writing; SSE stream (reuses the
    chat event pattern: `run_started` → `answer_delta` → `done`/`error`)
- **Architecture**: each agent is `agents/<name>.py` with a versioned
  prompt in `agents/prompts/<name>.md`, built by
  `build_<name>_agent(model, ...)`, per-request state via deps,
  module-level reusable agent — the QA reference implementation shape.
- **Model/config**: `get_chat_model` from `llm/models.py` (`CHAT_*`
  settings); no new provider plumbing.
- **No-key behavior**: like chat — `CHAT_API_KEY` missing ⇒ 503
  `chat_unavailable` from the deps constructor (all three need an LLM;
  no non-LLM fallback).
- **Services** (`services/agents.py` or per-agent services — child's
  choice, one file if it stays small): framework-free, agent runs +
  logging (`agent_run_started`/`finished`/`failed`, agent name
  distinguishes).
- **Tests**: FunctionModel everywhere; live smoke optional under
  `live_llm`; offline contract unchanged.

## Child task map

| Child | Slug | Core work | Data needs |
|---|---|---|---|
| 1. Summarize | `agent-summarize` | doc → chunks-aware summary (long-doc handling), sync endpoint on documents | document content (exists) |
| 2. Association | `agent-association` | related-documents analysis with rationale + similarity inputs (vector neighbors + tag overlap), JSON endpoint | retriever/chunk repo (exists) |
| 3. Writing | `agent-writing` | streaming suggestion endpoint grounded on optional KB context retrieval | retriever (exists) |

## Cross-child acceptance (parent-level)

- [ ] All three endpoints live and covered by tests consistent with the
      QA slice's bar (FunctionModel, probe-skip offline, envelope errors)
- [ ] Every agent: prompt asset versioned, deps-based state, no services
      imports in agents/, router thin, no-key 503, `agent_run_*` logging
- [ ] No schema changes anywhere in the suite
- [ ] Full gates green after each child; final full-suite run covers all
      three together

## Out of Scope (parent)

- MCP tool registration into these agents (can be added later via the
  existing `extra_tools` seam)
- Multi-turn context, session persistence
- Summaries/associations persisted as columns or documents (responses
  are computed on demand; persistence is a later decision)
