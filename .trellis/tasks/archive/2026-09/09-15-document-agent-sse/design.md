# Design: Document summary and associations SSE endpoints

Evidence anchors reference `main` @ `0207147`.

## 1. Architecture and boundaries

Layering is unchanged (`router → service → agent`); only the wire format at
the API boundary and the service delivery shape change.

- **Schemas** (new `src/app/schemas/agent_stream.py`): typed event models for
  the two agent streams (below). Chat event models in `schemas/chat.py` are
  NOT modified except that the shared `ErrorEvent` payload shape is reused
  (import the class, don't re-declare it).
- **Services** (`services/agents.py`): `SummarizeService` and
  `AssociationService` gain async-generator stream methods that yield typed
  events. The existing coroutine methods become thin wrappers that drain the
  generator and return the result payload, preserving every existing caller
  and test.
- **API** (`api/v1/endpoints/documents.py`): the two routes return
  `EventSourceResponse` using chat's priming idiom so pre-stream failures
  keep HTTP error envelopes.
- **Agents** (`agents/`): untouched; stay framework- and SSE-free.

## 2. Event contracts

Union `AgentStreamEvent` in `schemas/agent_stream.py`:

| Event name | Model | Payload fields |
|---|---|---|
| `run_started` | `AgentRunStartedEvent` | `run_id: str`, `kind: Literal["summary","associations"]`, `document_id: UUID` |
| `summary_progress` | `SummaryProgressEvent` | `phase: Literal["map_pass","reduce_pass"]`, `pass_index: int` (1-based), `passes_total: int` (map passes + 1) |
| `summary` | `SummaryResultEvent` | full `SummaryResult` (`document_id`, `summary`, `model`, `latency_ms`) |
| `associations` | `AssociationsResultEvent` | full `AssociationsResult` (`document_id`, `associations`, `model`, `latency_ms`) |
| `done` | `AgentDoneEvent` | `run_id: str`, `outcome: Literal["success"]`, `latency_ms: float` |
| `error` | `ErrorEvent` (imported from `schemas/chat.py`) | `code: str`, `message: str` |

Sequences:

- Summary success: `run_started` → `summary_progress`(map 1..N) →
  `summary_progress`(reduce) → `summary` → `done`. Single-pass summaries
  (≤ threshold) emit one `map_pass` progress event then `reduce_pass`
  (progress events describe orchestration phases even when trivially one
  pass, keeping the client grammar fixed).
- Associations success: `run_started` → `associations` → `done`. The
  structured association output stays atomic (no partial association events).
- Cache hit: `run_started` → `summary`/`associations` → `done` (no progress
  events, no model call; result identical to a computed run).
- Failure after HTTP 200: already-emitted events stand, then exactly one
  `error` event and stream close (service yields `ErrorEvent` and returns).

## 3. Streaming and error flow

- **Priming** (chat idiom, `api/v1/endpoints/chat.py:65-88`): endpoint pulls
  `first = await events.__anext__()` before constructing
  `EventSourceResponse`. Services load the document and check soft-delete
  BEFORE yielding `run_started`, so a missing/soft-deleted document raises
  `NotFoundError` pre-200 and keeps the existing 404 JSON envelope (AC4).
  Provider failures occur after the first yield and become terminal `error`
  events via the service's `_as_app_error` → `ErrorEvent(code, message)`
  mapping (same code strings as the HTTP envelope).
- **Serializer sharing** (R6): extract the chat name-map + `to_sse` pair
  into a small shared helper module (e.g. `api/v1/endpoints/sse.py`) with
  `to_sse(events, event_names)`; chat's existing `to_sse`/`to_sse_event`
  delegate to it (chat event names unchanged; chat tests must stay green).
  The new endpoints pass the `AgentStreamEvent` name map.
- **Tenant/RBAC**: routes keep `TenantScope` (read-level — no mutation,
  matching current wiring in `api/v1/endpoints/documents.py:79-92`).
  `document_id` + tenant flow into the services unchanged.

## 4. Service refactoring shape

- `SummarizeService.summarize_document_stream(doc_id, *, tenant_id) ->
  AsyncIterator[AgentStreamEvent]`: load doc (404 gate) → `run_started` →
  cache hit? `summary`+`done` : per-map-pass `summary_progress` → reduce
  `summary_progress` → `summary` → `done`. Latency accounting unchanged
  (`latency_ms` measured around the work as today, `services/agents.py:95-229`).
- `AssociationService.associate_document_stream(...)`: load (404 gate) →
  `run_started` → gather → no-candidates or cache-hit shortcut → single LLM
  call → `associations` → `done`.
- Coroutine wrappers drain the generator, capture the result event, return
  it; `error`-terminated streams re-raise the original AppError so the sync
  endpoints and tests keep their exact HTTP behavior.
- `run_id`: `uuid4().hex` per stream; log events carry ids/lengths/error
  classes only (logging spec; AC7).

## 5. Compatibility and rollout

- No schema/migration changes; no persistence changes; cache keys/TTLs
  unchanged. Existing synchronous response shapes are preserved verbatim
  inside the `summary`/`associations` payloads (R2), so a client that only
  wants the result reads one event.
- Rollback: revert the endpoint handlers to the coroutine calls; service
  wrappers keep the old method surface working throughout, so rollback is a
  two-file revert.

## 6. Trade-offs

- Fixed progress grammar (always map+reduce progress events) vs conditional
  emission: chosen for a stable client state machine; cost is two extra
  events on short summaries.
- Reusing chat's `ErrorEvent` vs a new agent error model: reuse avoids a
  second error envelope dialect on the wire; cost is a cross-module import,
  acceptable since `schemas/` is shared by design.
- Wrapping (keep coroutine API) vs converting all callers: wrapping keeps
  blast radius at the two endpoints and existing tests untouched.
