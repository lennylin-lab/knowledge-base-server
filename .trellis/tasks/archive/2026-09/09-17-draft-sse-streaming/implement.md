# Implementation Plan

1. Add `OperationDraftEvent` to `schemas/agent_stream.py` (additive; reuse existing event types).
2. Convert `AgentOperationService.draft_document` to an async generator: persist `running` → run structured model call → persist terminal state → yield draft event → yield done; on failure persist `failed` → yield single `error` event.
3. Rewire `POST /operations/draft` endpoint to SSE via the priming pattern and the shared stream plumbing.
4. Update `tests/test_operations_api.py` for event-order, wire-shape, persistence-timing, and failure coverage; keep chat-history-exclusion assertions.
5. Run `uv run ruff check .`, `uv run mypy src`, focused tests, then full `uv run pytest`; verify existing SSE tests pass byte-identically.

Review gates: no success event before terminal state is committed; exactly one terminal event per stream; pre-stream failures stay HTTP errors; existing streams unchanged.
