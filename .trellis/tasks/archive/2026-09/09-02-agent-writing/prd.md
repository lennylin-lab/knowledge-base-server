# Writing Assistant Agent with SSE Suggest Endpoint (child of builtin-agents)

## Goal

Final child. `POST /api/v1/writing/suggest` streams writing assistance
grounded in the knowledge base: the user submits draft text (and an
optional instruction), the agent may retrieve KB context via the
retrieval tool, and suggestions stream back over SSE — reusing the chat
event pattern (`run_started` → `sources` → `answer_delta` → `done` |
`error`). Closes the built-in agent suite with the second streaming
agent, this time retrieval-optional rather than retrieval-first.

Follow the parent's shared shape decisions verbatim.

## Requirements

1. **Prompt** (`agents/prompts/writing.md`): assistant for authoring and
   editing knowledge documents; when `search_knowledge` results are
   present, ground suggestions in them and label borrowed points with
   bracket citations like QA; when none, assist from general competence
   and say so; preserve the user's language and voice; never invent KB
   content that wasn't retrieved; respect the user's instruction
   (continue / rewrite / expand / critique, default: continue + improve).
2. **Agent** (`agents/writing.py`): `build_writing_agent(model)` with
   the `search_knowledge` tool registered (same closure/deps pattern as
   QA — per-request state via `WritingDeps`, module-level reusable
   agent; extra_tools seam for MCP later). Retrieval is OPTIONAL: the
   model decides whether to search; zero tool calls is a valid run.
3. **Service** (`services/agents.py` extension): `suggest(draft,
   instruction, limit?)` async generator yielding the chat SSE event
   vocabulary (`run_started`, `sources`, `answer_delta`, `done`,
   terminal `error`); run_id binding, never raise after first event,
   `agent_run_*` logging with `agent="writing"`. Draft text never
   logged (draft_length only).
4. **Endpoint**: `POST /api/v1/writing/suggest` (own router
   `api/v1/endpoints/writing.py` — first route in a new namespace),
   request `{draft: str (min 1, max ~50k chars), instruction: str | None,
   limit?: int (1..20, default 8)}` → SSE via EventSourceResponse.
   422s on empty/oversized draft; 503 `chat_unavailable` no-key.
5. **Tests**: offline service tests with FunctionModel + StubRetriever
   (with-tool and zero-tool scripted paths, mid-stream provider failure
   → terminal error, run_id in events+logs); SSE endpoint contract
   (event order, 422s, 503); one db+es integration test — seeded
   corpus, scripted model calls the real retrieval tool, `sources`
   reflects the corpus; offline contract; live smoke optional under
   `live_llm`.

## Out of Scope

- Applying suggestions back into documents (no write path — output is
  advisory text the user copies)
- Document-side diffs/patches, tone profiles, per-user style memory
- MCP tools in this agent (extra_tools seam exists; wiring is later)
- Multi-turn drafting sessions

## Acceptance Criteria

- [ ] With-tool path: scripted model calls `search_knowledge`, then
      streams text — SSE observed `run_started` → `sources` (retrieval
      results) → `answer_delta`+ → `done` with tool_calls=1 (db+es
      integration against seeded corpus for the real-retrieval variant)
- [ ] Zero-tool path: scripted model answers without searching — stream
      has `run_started` → `answer_delta`+ → `done`, NO `sources` event,
      tool_calls=0
- [ ] Provider failure mid-stream → terminal `error` event, stream
      closes, no `done`
- [ ] 422 on empty draft / >50k chars / bad limit; 503 envelope no-key
      before the stream starts
- [ ] `agent="writing"` run lifecycle logged; draft text and instruction
      never logged (lengths only)
- [ ] No regression: existing suites untouched and green; offline
      contract intact; gates green
