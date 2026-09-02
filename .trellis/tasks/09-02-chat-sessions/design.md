# Design: Multi-Turn Chat with Session Persistence

## Data model

```python
# models/chat.py
class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"

class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_updated_at_id", text("updated_at DESC, id DESC")),
    )
    id: UUID pk (uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="New chat")
    owner_id: Mapped[uuid.UUID | None]   # reserved
    created_at / updated_at (tz, server_default func.now(), onupdate)
    deleted_at: Mapped[datetime | None]

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )
    id: UUID pk
    session_id: FK("chat_sessions.id", ondelete="CASCADE")
    role: SAEnum(MessageRole, name="chat_message_role", values_callable=...)
    content: Text
    run_id: UUID | None
    created_at (tz, server_default func.now())
```

Migration `0004`: **enum lifecycle per database-guidelines** — explicit
`sa.Enum("user","assistant", name="chat_message_role").create(bind,
checkfirst=True)` + `postgresql.ENUM(..., create_type=False)` in the
column + explicit `drop(checkfirst=True)` in downgrade. Verify the
upgrade→downgrade→upgrade round trip.

## Repositories (`repositories/chat.py`)

`ChatSessionRepository`: `create`, `get_by_id` (soft-delete filtered),
`list_page` (keyset on `(updated_at DESC, id DESC)` — copy the documents
tuple_ pattern), `touch` (updated_at already advances via onupdate on
any message insert; explicit touch only if needed), `soft_delete`.

`ChatMessageRepository`: `add` (flush, no commit), `list_for_session`
(chronological, soft-delete filtered via join — messages belong to a
session; session-level visibility governs), `list_recent_for_session`
(newest-first bounded read for the history window).

Transactions owned by services (no commits in repositories).

## Services

`services/session.py` — `ChatSessionService`: session CRUD orchestration
(schemas, NotFoundError, structlog `session_created`/`session_deleted`
events; title derivation `_derive_title(question) -> str` — first
question, single line, truncated ~80 chars). Cursor codec: reuse the
documents service approach (same `{"ca","id"}` shape adapted to
updated_at — implementer may extract or copy the tiny codec; extraction
into `utils/` is justified NOW (second consumer) if clean).

`services/chat.py` — ChatService gains:
- ctor param `session_factory` (the established pattern: IndexingPipeline,
  SummarizeService). Opens one session per ask() for persistence.
- `ask(question, limit, session_id: UUID | None)`:
  1. If session_id: load-or-404 (BEFORE run_started — a 404 must be an
     envelope, not a stream error).
  2. Assemble history (below); persist the user message (own tx) — durable
     even if the run later fails. New session: create + title first,
     then persist user message.
  3. Run agent with `message_history` (pydantic-ai mechanism — build
     `ModelRequest`/`ModelResponse` parts from stored messages; verify
     exact constructors in 2.35; note deviation if a simpler
     prompt-embedding fallback is forced).
  4. On done: persist assistant message (full text, run_id) in a final tx.
  5. `run_started` gains `session_id: UUID | None`; `done` mirrors it.
  6. No session (None): EXACTLY today's behavior; no DB writes at all.
- Logging: `chat_message_persisted` (session_id, role, content_length —
  never content).

## History window (char budget)

```python
def select_history_window(messages: Sequence[ChatMessageRow],
                          *, budget: int) -> list[ChatMessageRow]:
    # messages newest-first; take whole TURNS (assistant+user pair) while
    # len(user)+len(assistant) fits budget; a turn that doesn't fit stops
    # the walk (no orphan half-turns); return oldest-first.
```

Pure function in `services/chat.py` (or utils if extracted) — unit-testable
offline. Settings: `CHAT_HISTORY_CHAR_BUDGET: int = 8000` (`.env.example`
entry — the env-example-completeness rule).

## API

- `api/v1/endpoints/sessions.py` — `sessions_router` prefix
  `/chat/sessions`, tags `["sessions"]`: GET `/` (cursor/limit), GET
  `/{id}` (detail + messages), DELETE `/{id}` (204). Thin, one service call.
- `chat.py` endpoint: `ChatRequest.session_id: UUID | None = None` —
  no signature change beyond passing it through.
- `schemas/session.py`: `SessionRead` (id, title, created_at, updated_at),
  `SessionDetail` (Read + `messages: [MessageRead]`), `MessageRead` (id,
  role, content, run_id, created_at), `SessionPage` (items+next_cursor).
- 404 ordering: session lookup happens inside ask() before the first
  event → NotFoundError propagates BEFORE the response starts → clean
  envelope (contrast: provider failures mid-stream are terminal events).
  Verify EventSourceResponse isn't opened before ask() starts.

## Error handling & logging

| Case | Behavior |
|---|---|
| session_id missing/soft-deleted | 404 envelope before stream starts |
| Run fails after user msg persisted | user msg kept, no assistant msg, terminal error event; session continuable |
| History read failure | SearchIndexError-like: propagates → 500/502 per AppError; DB is a hard dependency here |
| Content logging | lengths only, never content (sessions hold Q&A content = sensitive) |

## Tests

| Suite | World | Cases |
|---|---|---|
| `test_history_window.py` | offline pure | whole-turn selection, budget stop, no orphan half-turn, empty, single-turn-over-budget (drops it — oldest turn may be dropped entirely; define: if even the newest turn exceeds budget, pass NO history — the question stands alone) |
| `test_sessions_api.py` | db | CRUD contract, pagination ordering + disjoint cursors, detail chronological, soft-delete invisibility + 404 |
| `test_chat_service.py` extend | offline stubs + scripted model | session_id flow (new session + title), second turn sees history (assert captured message_history), failed run persists user only, no-session mode = zero DB writes (assert no repo calls) |
| `test_chat_api.py` extend | offline | run_started carries session_id; 404 envelope on missing session (no stream); regression: body without session_id accepted |
| migration cycle | db up | upgrade→downgrade→upgrade + pg_type check for chat_message_role |

## Tradeoffs / Rejected

- **Summarization compaction** (rejected, user decision): char budget
  now; `select_history_window` is the seam.
- **Persist assistant text on error/partial** (rejected): only complete
  answers are stored; partials mislead the next turn.
- **Message-level soft delete** (rejected): sessions are the deletion
  unit; message visibility follows session visibility.
- **pydantic-ai conversation/session storage** (rejected): our schema is
  the source of truth; the framework gets rebuilt ModelMessages per run.
- **Title via LLM** (rejected): truncate the first question; LLM titles
  are a cosmetic later task.
- **In-request session (get_db) for chat** (rejected): ChatService is
  process-lifetime (lru_cache); session_factory is the established
  pattern for lifetime-mismatched services.

## Rollback

Two commits (model+migration+repo / services+endpoints+wiring+tests).
Revert G2 restores stateless chat verbatim; `alembic downgrade -1` drops
the tables. No existing column changes.
