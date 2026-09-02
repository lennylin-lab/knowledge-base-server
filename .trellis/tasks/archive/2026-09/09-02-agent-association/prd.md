# Association Agent: Related-Documents Analysis (child of builtin-agents)

## Goal

Second child. `POST /api/v1/documents/{id}/associations` returns LLM-curated
related documents with rationale, grounded in two deterministic candidate
signals: vector similarity (pgvector neighbors of the document's chunks) and
tag overlap. Exercises the structured-output agent shape (validated result
type), which neither QA (streaming text) nor summarize (plain sync text)
covered.

Follow the parent's shared shape decisions verbatim (architecture, model,
no-key 503, service shape, FunctionModel tests).

## Requirements

1. **Candidate gathering (deterministic, pre-LLM)** — new repository
   methods, no business logic in them:
   - Vector leg: nearest chunks from OTHER live documents using this
     document's own chunk embeddings (average or per-chunk query —
     implementer's choice, justify; exclude self and soft-deleted via the
     established live-doc join), deduped to candidate documents, bounded
     (e.g. top 10).
   - Tag leg: live documents sharing ≥1 tag with the source (ARRAY
     overlap operator), excluding self, bounded (e.g. top 10 by
     recency).
   - Both legs return (document_id, title, tags, signal detail —
     e.g. similarity distance or shared tags) for the prompt.
2. **Prompt** (`agents/prompts/association.md`): given the source doc
   (title/tags/summary-of-content allowed: first chunk or bounded excerpt)
   and candidates with their signals, select the genuinely related ones,
   write a one-to-two-sentence reason each, in the source document's
   language; drop weak candidates rather than padding; never invent
   documents not in the candidate list.
3. **Agent** (`agents/association.py`): `build_association_agent(model)`
   with **structured output** (validated `AssociationsOutput` type:
   list of `{document_id, reason, strength?}`-ish) — pydantic-ai
   `output_type`; if the installed version's structured-output +
   FunctionModel scripting proves impractical, fall back to the
   documented pattern used by summarize (plain text + service-side
   construction) — note the deviation. No tools; candidates are
   supplied directly.
4. **Service** (`services/agents.py` extension or `services/association.py`
   if the file grows): `associate(doc_id)` — 404 on missing/soft-deleted
   before any work; gather candidates (embedding leg requires indexed
   content — if the doc has no chunks yet, degrade to tag-only; if no
   candidates at all, return an empty associations list without an LLM
   call); run the agent; join the LLM's selected ids back to candidate
   metadata (drop any hallucinated id not in the candidate set);
   `agent_run_*` logging with `agent="association"`.
5. **Endpoint**: `POST /api/v1/documents/{id}/associations` → 200
   `{document_id, associations: [{document_id, title, tags, reason,
   signal}], model, latency_ms}`; 404/503 per the established gates.
6. **Tests**: candidate gathering against seeded corpus (vector leg finds
   the seeded-neighbor doc; tag leg finds shared-tag doc; self excluded;
   soft-deleted invisible); agent selection with FunctionModel (incl.
   hallucinated-id dropping); no-candidates short-circuits without LLM;
   404; 503; embedding-degradation (tag-only when source unindexed);
   offline contract.

## Out of Scope

- Persisting associations (graph edges) — computed on demand
- Bidirectional/backlink maintenance, association strength tuning
- MCP tools or retrieval tool in this agent
- Pagination of associations

## Acceptance Criteria

- [ ] Seeded corpus: POST associations → 200; the related doc appears with
      a reason; every returned id was in the deterministic candidate set
- [ ] Vector leg and tag leg each verified independently against seeded
      data; self and soft-deleted documents never candidates
- [ ] Source with no chunks (unindexed) → tag-only candidates (no crash,
      no empty result if tags match); source with no candidates at all →
      empty list, zero LLM calls
- [ ] LLM-returned id not in candidates → dropped from the response
- [ ] 404 envelope missing/soft-deleted; 503 `chat_unavailable` no-key
- [ ] `agent_run_*` with agent="association", content/reasons not logged
      at info
- [ ] Offline contract intact; gates green
