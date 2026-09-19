# Draft model call streams via agent.run_stream to avoid gateway non-streaming timeout

## Goal

Switch the draft model call in `services/operation.py` (`_draft_events`, the `agent.run` call) to `agent.run_stream`, so the structured draft output is streamed from the provider and aggregated server-side. Streaming responses keep bytes flowing through knowledge-base-gateway, avoiding its hardcoded 60s whole-request non-streaming deadline (`internal/config/config.go:94`, not env-tunable).

## Background / Confirmed facts

- Draft behind the gateway still fails (`llm_provider_error`); gateway enforces `RequestTimeout: 60s` + `MaxRetries: 2` on non-streaming requests and exposes no env override.
- A full draft generation can exceed 60s; only streaming passes through the gateway's deadline.
- `agent.run_stream` with structured output (`DraftOutput`) still yields a final validated `result.output`; the atomic-draft contract (single `draft` event, no partial tokens on the wire) is preserved because aggregation happens inside the service.
- Persistence timing rule is unchanged: terminal state commits BEFORE the terminal event.

## Requirements

- Replace the `agent.run(...)` call in `_draft_events` with the `agent.run_stream` context manager; use the final validated output (`result.get_output()` or equivalent) — the SSE wire contract (`run_started` → `draft` → `done` / single `error`) must stay byte-identical.
- Tool/retrieval behavior (SourceCollector, tool_calls count) unchanged.
- Error handling unchanged: any failure inside the stream maps through `_as_app_error`, commits `failed`, and surfaces as the single terminal `error`.
- Update `tests/fakes.py` `scripted_draft_model` so the FunctionModel serves a streamed response (pydantic-ai FunctionModel supports streaming via a streamed response return); all existing draft tests must pass unchanged in their assertions.

## Acceptance Criteria

- [ ] No `agent.run` remains on the draft path.
- [ ] All draft API tests pass with unchanged event-order/persistence assertions.
- [ ] `uv run ruff check .`, `uv run mypy src`, full `KB_OIDC_ISSUER= uv run pytest` pass.

## Out of scope

- Gateway-side timeout policy changes (separate task, different repo).
- Emitting partial draft tokens to the client.
