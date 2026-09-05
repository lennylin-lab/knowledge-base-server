"""Chat orchestration: run_id binding, session persistence, agent streaming,
SSE event mapping.

Multi-turn with session persistence when a `session_factory` is wired: the
user message is persisted before the run (durable even if the run fails),
the assistant message on success (full streamed text + run_id), and the
recent conversation returns to the agent as `message_history` within a
character budget. Without a factory the service is exactly the original
stateless single-turn chat — no database access at all.

Failures after the first event become a terminal `error` event — nothing may
escape `ask` once streaming has started (error-handling spec, streaming rule).
The one exception is the session prelude: it runs before the first event, so
a missing/soft-deleted session raises `NotFoundError` eagerly and the caller
answers with a clean 404 envelope instead of a broken stream.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

import openai
import structlog
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.qa import ChatDeps, SourceCollector, build_qa_agent
from app.core.exceptions import (
    AppError,
    LLMProviderError,
    LLMRateLimitedError,
    NotFoundError,
)
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.rag.retriever import Retriever
from app.repositories.chat import ChatMessageRepository, ChatSessionRepository
from app.schemas.chat import (
    AnswerDeltaEvent,
    ChatStreamEvent,
    DoneEvent,
    ErrorEvent,
    RunStartedEvent,
    SearchMode,
    SourcesEvent,
)
from app.schemas.search import SearchHit
from app.services.session import derive_title

logger = structlog.get_logger(__name__)

# DB-read bound for the history window: enough newest messages that the char
# budget — not the read — is what limits what reaches the model.
HISTORY_READ_LIMIT = 200


def _complete_turns_newest_first(
    messages: Sequence[ChatMessage],
) -> list[tuple[ChatMessage, ChatMessage]]:
    """Group a newest-first message list into complete (user, assistant) turns.

    `user` is the older message of each pair. A leading user message (a failed
    run's unanswered question) is not a complete turn and is skipped; any
    shape that stops pairing cleanly (e.g. two users in a row) ends the walk.
    History is made of complete exchanges only — never an orphan half-turn.
    """
    turns: list[tuple[ChatMessage, ChatMessage]] = []
    index = 1 if messages and messages[0].role is MessageRole.USER else 0
    while index + 1 < len(messages):
        assistant, user = messages[index], messages[index + 1]
        if assistant.role is not MessageRole.ASSISTANT or user.role is not MessageRole.USER:
            break
        turns.append((user, assistant))
        index += 2
    return turns


def select_history_window(messages: Sequence[ChatMessage], *, budget: int) -> list[ChatMessage]:
    """Pick the newest complete turns whose combined content fits `budget`.

    Input is newest-first (the repository's bounded read); the walk takes
    whole turns while they fit and stops at the first one that does not —
    no orphan half-turns. If even the newest turn exceeds the budget the
    result is empty: the question stands alone. Output is oldest-first,
    ready to become `message_history`.
    """
    window: list[ChatMessage] = []
    remaining = budget
    for user, assistant in _complete_turns_newest_first(messages):
        cost = len(user.content) + len(assistant.content)
        if cost > remaining:
            break
        # Newest-first append (assistant, then its user); reversed at the end.
        window.append(assistant)
        window.append(user)
        remaining -= cost
    window.reverse()
    return window


def to_message_history(messages: Sequence[ChatMessage]) -> list[ModelMessage]:
    """Rebuild pydantic-ai `message_history` from stored role/content rows.

    Stored messages are plain (role, content) pairs — tool traffic and
    citations are per-run, not conversation, state — so each row maps to one
    ModelRequest (user) or ModelResponse (assistant) carrying its text.
    """
    history: list[ModelMessage] = []
    for message in messages:
        if message.role is MessageRole.USER:
            history.append(ModelRequest(parts=[UserPromptPart(content=message.content)]))
        else:
            history.append(ModelResponse(parts=[TextPart(content=message.content)]))
    return history


@dataclass(slots=True)
class _ChatTurn:
    """Persistence context for one `ask` run (absent in stateless mode)."""

    session_id: UUID
    history: list[ModelMessage]


class ChatService:
    """Streams one knowledge-grounded answer per question."""

    def __init__(
        self,
        retriever: Retriever,
        model: Model,
        *,
        mode: SearchMode,
        extra_tools: Sequence[Tool[ChatDeps]] = (),
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        history_char_budget: int = 8000,
    ) -> None:
        self._retriever = retriever
        self._mode = mode
        self._model = model
        # None = stateless mode: exactly the pre-session service, no DB writes.
        self._session_factory = session_factory
        self._history_char_budget = history_char_budget
        # Wrapped MCP tools arrive here (wired once per process in deps.py);
        # the default empty sequence keeps the agent identical to pre-MCP.
        self._agent = build_qa_agent(model, extra_tools=extra_tools)

    async def ask(
        self, question: str, *, limit: int = 8, session_id: UUID | None = None
    ) -> AsyncIterator[ChatStreamEvent]:
        """Run one QA turn, yielding typed stream events in contract order.

        `run_id` is bound into structlog contextvars for the whole generator,
        so every log line of the run (including retrieval legs inside tool
        calls) carries it; a resolved session adds its id. The question text
        itself is never logged. With `session_id`: the turn is persisted and
        history informs the model (it never enters `sources` — citations stay
        run-local). Without a wired session factory, nothing here touches
        the database.
        """
        run_id = uuid4().hex
        structlog.contextvars.bind_contextvars(run_id=run_id)
        started = time.perf_counter()

        # Session prelude — BEFORE the first event: a missing session must
        # surface as a 404 envelope, never as a mid-stream failure.
        turn = await self._prepare_turn(question, session_id)
        if turn is not None:
            structlog.contextvars.bind_contextvars(session_id=str(turn.session_id))

        # The service owns the flush hook: the agent appends, the service
        # drains. Keeps `agents/` free of any streaming knowledge.
        pending: list[list[SearchHit]] = []
        collector = SourceCollector(on_append=pending.append)
        deps = ChatDeps(retriever=self._retriever, limit=limit, collector=collector)

        logger.info("agent_run_started", agent="qa", question_length=len(question))
        yield RunStartedEvent(
            run_id=run_id, mode=self._mode, session_id=turn.session_id if turn else None
        )

        usage_input_tokens: int | None = None
        usage_output_tokens: int | None = None
        answer_parts: list[str] = []
        try:
            async with self._agent.run_stream(
                question,
                deps=deps,
                # Empty history passes None: a first turn behaves exactly like
                # the stateless service (no empty-sequence edge cases).
                message_history=turn.history if turn and turn.history else None,
            ) as result:
                async for delta in result.stream_text(delta=True, debounce_by=None):
                    # Tool calls (and their sources) can land between parts;
                    # drain before the part so sources always precede the text
                    # they ground.
                    for batch in _drain(pending):
                        yield SourcesEvent(items=batch)
                    if delta:
                        answer_parts.append(delta)
                        yield AnswerDeltaEvent(text=delta)
                usage = result.usage
                usage_input_tokens = usage.input_tokens or None
                usage_output_tokens = usage.output_tokens or None
            for batch in _drain(pending):
                yield SourcesEvent(items=batch)
            # Inside the try on purpose: the stream has started, so a persist
            # failure must become the terminal `error` event (never escape the
            # generator) — and it leaves the honest record: user message kept,
            # no assistant message, session continuable.
            if turn is not None:
                await self._persist_assistant_message(
                    turn, content="".join(answer_parts), run_id=run_id
                )
        except Exception as exc:
            failure = _as_app_error(exc)
            logger.exception(
                "agent_run_failed",
                agent="qa",
                outcome=failure.code,
                error_class=type(exc).__name__,
                tool_calls=collector.tool_calls,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            yield ErrorEvent(code=failure.code, message=failure.message)
            return

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "agent_run_finished",
            agent="qa",
            model=self._model.model_name,
            outcome="success",
            tool_calls=collector.tool_calls,
            latency_ms=latency_ms,
            input_tokens=usage_input_tokens,
            output_tokens=usage_output_tokens,
        )
        yield DoneEvent(
            run_id=run_id,
            outcome="success",
            tool_calls=collector.tool_calls,
            latency_ms=latency_ms,
            session_id=turn.session_id if turn else None,
        )

    async def _prepare_turn(self, question: str, session_id: UUID | None) -> _ChatTurn | None:
        """Resolve the session, assemble history, persist the user message.

        One transaction: new sessions are created here (title derived from
        the question, no LLM call), existing ones are loaded or raise
        `NotFoundError` (missing/soft-deleted). The user message is durable
        before the run starts — a failed run leaves it as the honest record.
        History is read BEFORE the user message is added: the current
        question reaches the model as the run prompt, not as history.
        """
        if self._session_factory is None:
            return None
        async with self._session_factory() as session:
            sessions = ChatSessionRepository(session)
            messages = ChatMessageRepository(session)
            if session_id is None:
                chat_session = await sessions.create(ChatSession(title=derive_title(question)))
                logger.info(
                    "session_created", session_id=str(chat_session.id), title=chat_session.title
                )
                history_rows: Sequence[ChatMessage] = ()
            else:
                existing = await sessions.get_by_id(session_id)
                if existing is None:
                    raise NotFoundError(f"Chat session {session_id} not found")
                chat_session = existing
                history_rows = select_history_window(
                    await messages.list_recent_for_session(session_id, limit=HISTORY_READ_LIMIT),
                    budget=self._history_char_budget,
                )
            await messages.add(
                ChatMessage(session_id=chat_session.id, role=MessageRole.USER, content=question)
            )
            await sessions.touch(chat_session.id)
            await session.commit()
            logger.info(
                "chat_message_persisted",
                session_id=str(chat_session.id),
                role=MessageRole.USER.value,
                content_length=len(question),
            )
            return _ChatTurn(session_id=chat_session.id, history=to_message_history(history_rows))

    async def _persist_assistant_message(
        self, turn: _ChatTurn, *, content: str, run_id: str
    ) -> None:
        """Persist the complete assistant answer (own transaction, on done)."""
        if self._session_factory is None:
            # Unreachable from `ask` (a turn exists only with a factory); the
            # guard keeps the method total rather than asserting.
            return
        async with self._session_factory() as session:
            messages = ChatMessageRepository(session)
            await messages.add(
                ChatMessage(
                    session_id=turn.session_id,
                    role=MessageRole.ASSISTANT,
                    content=content,
                    run_id=UUID(hex=run_id),
                )
            )
            await ChatSessionRepository(session).touch(turn.session_id)
            await session.commit()
            logger.info(
                "chat_message_persisted",
                session_id=str(turn.session_id),
                role=MessageRole.ASSISTANT.value,
                content_length=len(content),
            )


def _drain(pending: list[list[SearchHit]]) -> list[list[SearchHit]]:
    """Take all pending retrieval batches, leaving the buffer empty."""
    batches = list(pending)
    pending.clear()
    return batches


def _as_app_error(exc: Exception) -> AppError:
    """Map a streaming failure onto the error taxonomy for the error event.

    Provider failures surface from the agent run wrapped in pydantic-ai's own
    types (`ModelHTTPError` for HTTP >= 400, `ModelAPIError` for
    connection/timeout — the production model never lets raw SDK exceptions
    through) or, on paths that bypass pydantic-ai, as raw SDK exceptions;
    both are wrapped here. `AppError` subclasses (e.g. `SearchIndexError`
    raised inside the retrieval tool) already carry the right code/message.
    Unknown exceptions get the generic internal error — details go to logs,
    never to the stream.
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, ModelHTTPError):
        if exc.status_code == 429:
            return LLMRateLimitedError("LLM provider rate limit exceeded")
        return LLMProviderError("LLM provider request failed")
    if isinstance(exc, ModelAPIError):
        return LLMProviderError("LLM provider request failed")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitedError("LLM provider rate limit exceeded")
    if isinstance(exc, openai.APIError):
        return LLMProviderError("LLM provider request failed")
    return AppError("Internal server error")
