# Implement: Multi-Turn Chat Sessions

Ordered checklist. Gates after each group; commit points G1/G2.

## G1 — Model + migration + repositories

- [ ] `models/chat.py` (ChatSession, ChatRole per design) + `models/__init__` re-export
- [ ] `alembic revision --autogenerate -m "chat sessions and messages"` → hand-edit:
      enum lifecycle (explicit create checkfirst + postgresql.ENUM
      create_type=False + explicit drop), FK CASCADE, both indexes,
      working downgrade
- [ ] `repositories/chat.py`: ChatSessionRepository (create/get_by_id/
      list_page keyset/soft_delete), ChatMessageRepository (add/
      list_for_session/list_recent_for_session) — no commits
- [ ] Validation: KB_DATABASE_URL=postgresql+asyncpg://kb:kb@localhost:5432/kb
      `uv run alembic upgrade head` → `downgrade -1` → `upgrade head`;
      pg_type check for chat_message_role; mypy + ruff

**Commit G1** — "feat: chat_sessions/chat_messages model, migration, repositories"

## G2 — Services, history window, endpoints, wiring

- [ ] `core/config.py`: CHAT_HISTORY_CHAR_BUDGET (default 8000) + `.env.example`
- [ ] `select_history_window` pure function + `tests/test_history_window.py`
- [ ] `services/session.py`: ChatSessionService (CRUD, title derivation,
      session events) + `schemas/session.py`
- [ ] `services/chat.py`: session_factory ctor, session_id param on ask,
      history assembly (pydantic-ai message_history — verify 2.35
      constructors), user-msg-before-run / assistant-msg-on-done
      persistence, run_started/done carry session_id; no-session mode
      writes nothing
- [ ] `api/v1/endpoints/sessions.py` (3 routes) + router registration;
      ChatRequest.session_id; endpoint unchanged otherwise
- [ ] Tests: test_sessions_api.py, test_chat_service.py extensions
      (history visible turn 2, failed-run persistence, zero-writes
      stateless mode), test_chat_api.py extensions
- [ ] Validation: compose up full `uv run pytest`; offline spot check;
      full gates

**Commit G2** — "feat: multi-turn chat with session persistence and history window"

## Review gates

- trellis-check dispatch after G2: five spec files, PRD acceptance sweep,
  migration round trip + enum lifecycle, stateless regression, gates.

## Rollback

- After G1: `alembic downgrade -1` + revert
- After G2: revert restores stateless chat verbatim
