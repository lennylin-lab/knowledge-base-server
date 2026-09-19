# Stream summary and association endpoints token-by-token

## Problem

`POST /documents/{id}/summary` and `POST /documents/{id}/associations` already
return SSE, but the LLM calls inside the services are non-streaming
(`Agent.run`): the summary arrives as one atomic `summary` event only after the
model finished, and associations arrive as one `associations` event after the
whole structured output is generated. Users stare at a blank result for the
full generation time.

## Goal

True incremental streaming:

- **Summary**: the final summary pass streams its text to the client as it is
  generated (token deltas), instead of arriving atomically.
- **Associations**: each curated association item is pushed as its own event as
  soon as the model's partial JSON stream yields it — the list "fills in"
  live rather than appearing at once.

## Requirements

1. New additive SSE events (wire contract extension, no breaking change):
   - `summary_delta` — a verbatim text fragment of the final summary pass.
     Concatenation of all deltas equals the `summary` field of the final
     `summary` result event.
   - `association_item` — one complete curated item (`document_id`, `title`,
     `tags`, `reason`, `signal`, plus 1-based position). The final
     `associations` result event still carries the full payload unchanged.
2. Map passes of a multi-chunk summary stay non-streaming (their output feeds
   the reduce pass, not the client); only the user-visible final pass streams.
   `summary_progress` events keep the existing fixed grammar.
3. Cache hits and the no-candidates shortcut keep their existing shape (no
   deltas/items — they never ran a model).
4. Existing invariants preserved: 404-before-first-yield envelope priming,
   terminal `error` event after first yield, terminal `done`, never-persisted
   results, sync drain wrappers unchanged.
5. Association items pushed early MUST be identical to the items in the final
   result event (deterministic metadata join happens before emission; LLM ids
   that are not candidates are dropped and never streamed).

## Acceptance criteria

- [ ] A client concatenating `summary_delta` payloads then receiving `summary`
      gets identical text.
- [ ] Association items arrive incrementally (observable with a scripted model)
      and match the final `associations` payload.
- [ ] All existing tests still pass; new tests cover delta/item emission,
      cache-hit shortcut, and error-during-stream (terminal `error`, no
      partial result event).
- [ ] `uv run ruff check`, `uv run mypy`, `uv run pytest` green.
