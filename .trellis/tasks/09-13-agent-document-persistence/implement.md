# Implementation Plan

1. Define run/draft/revision schemas, enums, repositories, and idempotency key.
2. Add models and Alembic migration with relevant indexes.
3. Implement operation service and atomic optimistic-concurrency apply.
4. Add structured writing-agent tool output and service wiring.
5. Add inspect/apply/resume API endpoints and error mapping.
6. Reuse reindex enqueue and add transition/conflict logs.
7. Test interruption, history exclusion, stale version, idempotent retry, enqueue, and provider failure.
8. Run `uv run ruff check .`, `uv run mypy src`, focused tests, then full `uv run pytest`.

Review gates: no partial draft in chat_messages; stale apply cannot publish; duplicate apply creates no second revision; rollback path documented.
