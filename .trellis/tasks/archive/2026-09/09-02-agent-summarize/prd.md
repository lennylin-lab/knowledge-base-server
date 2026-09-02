# Summarize Agent + Sync Endpoint (child of builtin-agents)

## Goal

First child of the builtin-agents parent. `POST /api/v1/documents/{id}/summary`
returns an LLM-generated summary of the document, synchronously. Exercises
the non-streaming agent shape (structured single response), which the QA
slice did not cover.

Follow the parent's shared shape decisions verbatim (endpoints,
architecture, model, no-key, services, tests).

## Requirements

1. **Prompt** (`agents/prompts/summarize.md`): concise summary in the
   document's language; preserve key entities/decisions; note the
   document's tags context is available but the summary must stand
   alone; bounded length guidance (e.g. ≤300 words target, hard cap
   via model output).
2. **Agent** (`agents/summarize.py`): `build_summarize_agent(model)`,
   per-request state via deps (document title/tags into the prompt
   context), **no tools** — the document content is supplied directly;
   no retrieval in v1.
3. **Long-document handling**: content is chunked with the existing
   `rag/chunker.py`; if it fits one chunk (< ~2 model windows of text),
   summarize directly; otherwise **map-reduce** — chunk summaries
   (single LLM pass each, sequential or small-batch), then a final
   reduce pass. Keep it simple and deterministic; no parallel fan-out
   in v1.
4. **Service** (`services/agents.py`): `summarize_document(doc_id)` —
   loads the document (NotFoundError on missing/soft-deleted), runs
   the agent, logs `agent_run_*` with `agent="summarize"`. Returns a
   schema, never ORM.
5. **Endpoint** on the documents router (`api/v1/endpoints/documents.py`
   or a dedicated `summaries.py` — implementer's choice, justify):
   `POST /documents/{id}/summary` → 200 `{document_id, summary,
   model, latency_ms}`; 404 envelope on missing doc; 503 when chat
   unconfigured.
6. **Tests**: FunctionModel for both direct and map-reduce paths
   (multi-request scripting); 404; 503 no-key; long-doc path chunking
   actually exercised (scripted per-chunk outputs); live smoke optional
   under `live_llm`.

## Out of Scope

- Persisting the summary (column/document)
- Retrieval-augmented or MCP tools in this agent
- Streaming (sync endpoint by parent decision)
- Summary length/format options in the request body (v1: no body)

## Acceptance Criteria

- [ ] Short doc: POST summary → 200 with a summary from the scripted
      model; `model` echoes the configured CHAT_MODEL; latency present
- [ ] Long doc (content forcing >1 chunk): map-reduce path runs ≥2
      FunctionModel requests and returns the reduce output
- [ ] Missing/soft-deleted doc → 404 envelope; no CHAT_API_KEY → 503
      envelope before any LLM call
- [ ] `agent_run_started`/`agent_run_finished` logged with
      `agent="summarize"`, doc id bound, content never logged
- [ ] Offline contract intact; gates green
