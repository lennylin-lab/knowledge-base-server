# Multi-Turn Chat with Session Persistence

## Goal

Upgrade chat from single-turn stateless to persistent multi-turn
conversations: sessions and messages are stored in PostgreSQL, chat
requests can continue an existing session, and the agent receives the
recent conversation history. Users can list, inspect, and soft-delete
their sessions.

User decisions: **full session endpoints** (list/get/delete + implicit
create); **character-budget history window** (complete turns, newest
first, bounded — no summarization compaction in MVP).

## Requirements

1. **Data model** + migration `0004`:
   - `chat_sessions`: UUID pk, `title` (derived from the first user
     message, truncated), `owner_id` (nullable, reserved per project
     convention), `created_at`/`updated_at` (tz, server defaults),
     `deleted_at` (nullable — soft delete, consistent with documents).
   - `chat_messages`: UUID pk, `session_id` FK → `chat_sessions.id`
     `ondelete=CASCADE`, `role` enum (`user` | `assistant` — **follow the
     documented enum lifecycle in database-guidelines**), `content`
     (Text), `run_id` (UUID, nullable — links to the agent run that
     produced assistant messages), `created_at` (tz, server default).
     Index `(session_id, created_at)` for history reads.
2. **Session endpoints** (new `api/v1/endpoints/sessions.py` or extend
   chat's — implementer's choice, justify):
   - `GET /api/v1/chat/sessions` — keyset-paginated list (reuse the
     documents cursor pattern; order `updated_at DESC, id DESC`;
     limit ≤100 default 20)
   - `GET /api/v1/chat/sessions/{id}` — session detail **with its
     messages** in chronological order
   - `DELETE /api/v1/chat/sessions/{id}` — soft delete → 204; deleted
     sessions are invisible everywhere and 404 on direct access
3. **Chat integration**:
   - `ChatRequest` gains optional `session_id: UUID | None`. Absent ⇒
     create a new session (title = first question truncated to ~80
     chars, no LLM call for the title); present ⇒ continue it
     (404 `not_found` if missing/soft-deleted — before the stream starts).
   - `run_started` event carries `session_id`; `done` may too.
   - **History assembly (char-budget window)**: load the session's
     messages, take complete turns newest-first until the budget
     (Settings `CHAT_HISTORY_CHAR_BUDGET`, default 8000) is exhausted,
     pass oldest-first as the agent's message history (pydantic-ai
     `message_history` — verify the exact mechanism; if it requires
     `ModelMessage` objects, build them from stored role/content).
   - **Persistence**: user message persisted before the run (durable
     even if the run fails); assistant message persisted on `done` (full
     text + run_id). A failed run leaves the user message and no
     assistant message — the honest record.
   - Session `updated_at` advances on each turn.
4. **History correctness**: the retrieval tool and citation behavior are
   unchanged — history informs the question, it does not enter `sources`.
5. **Tests**: session CRUD contract (list pagination ordering + cursor
   stability, detail with chronological messages, soft-delete
   invisibility + 404); chat with session — new session created and
   returned in events; second turn sees history (assert via scripted
   model capturing the prompt/message_history); failed run persists user
   message only; history budget truncation drops oldest turns and never
   splits a turn's pair in the middle (or if it must split, drop the
   orphan — define and test); 404 on continuing missing/deleted session;
   regression — chat WITHOUT session_id behaves exactly as today
   (stateless mode still works; nothing persisted).

## Out of Scope

- Summarization compaction of older history (later; summarize agent is
  the seam)
- Sessions for writing/other agents
- Message-level edit/delete, forking, sharing
- Streaming partial assistant text persistence (only on done)
- Token-accurate budgeting (chars are the MVP proxy)

## Acceptance Criteria

- [ ] Migration `0004` upgrade → downgrade → upgrade round-trips; enum
      `chat_message_role` follows the explicit lifecycle (no
      DuplicateObjectError)
- [ ] Sessions list/detail/delete behave per requirement 2 (tested,
      incl. cursor disjoint pages and soft-delete invisibility)
- [ ] Chat without session_id: identical behavior to today (regression
      suite green); nothing persisted to chat tables
- [ ] Chat with session_id: `run_started` carries it; two-turn scripted
      conversation — the second turn's agent input includes the first
      turn's user+assistant messages; both messages persisted
      chronologically; session title derived from the first question
- [ ] Failed run (scripted provider failure): user message persisted,
      no assistant message; session still continuable
- [ ] History budget: a session exceeding `CHAT_HISTORY_CHAR_BUDGET`
      truncates oldest complete turns (tested with a small budget);
      budget configurable via Settings + `.env.example` entry
- [ ] Offline (compose down): `uv run pytest` green with visible skips
- [ ] Gates green: ruff check / ruff format --check / mypy src / pytest
