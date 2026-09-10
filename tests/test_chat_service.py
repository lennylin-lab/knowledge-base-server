"""Chat service stream semantics: event order, sources, failures, sessions.

The model is always a scripted FunctionModel and the retriever a stub — no
live LLM, no infrastructure. Provider failures are scripted with the openai
SDK's own exception types so the service's error mapping is exercised for
real. Session-persistence tests run against the disposable test database
(`db`-marked) with the service wired to the per-test session factory — the
production lifetime pattern.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import openai
import pytest
import structlog
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.agents.qa import SourceCollector
from app.core.exceptions import NotFoundError, SearchIndexError
from app.mcp.manager import McpToolInfo, McpToolResult
from app.mcp.tools import build_agent_tools
from app.models.chat import ChatMessage, MessageRole
from app.rag.retriever import RetrievedChunk, SearchOutcome
from app.repositories.chat import ChatMessageRepository, ChatSessionRepository
from app.schemas.chat import (
    AnswerDeltaEvent,
    DoneEvent,
    ErrorEvent,
    RunStartedEvent,
    SourcesEvent,
)
from app.services.chat import ChatService
from app.services.session import derive_title
from fakes import (
    FakeMcpManager,
    StubRetriever,
    retrieved_chunk,
    scripted_chat_model,
    scripted_rewrite_model,
)

QUESTION = "What do the notes say about zorblat?"
ANSWER_PARTS = ["Zorblat is a test term ", "used in fixtures [1]."]


def _make_retriever(item: RetrievedChunk | None = None) -> StubRetriever:
    return StubRetriever(
        outcome=SearchOutcome(
            mode="hybrid",
            items=[item if item is not None else retrieved_chunk()],
            es_hits=1,
            vector_hits=1,
        )
    )


async def _collect(
    service: ChatService, question: str, *, limit: int = 8, session_id: UUID | None = None
) -> list[object]:
    return [event async for event in service.ask(question, limit=limit, session_id=session_id)]


def _names(events: list[object]) -> list[str]:
    return [type(event).__name__ for event in events]


def _answer_text(events: list[object]) -> str:
    return "".join(event.text for event in events if isinstance(event, AnswerDeltaEvent))


async def test_ask_streams_run_started_sources_deltas_done_in_order():
    retriever = _make_retriever(
        retrieved_chunk(content="zorblat everywhere", document_title="Kotlin Notes")
    )
    service = ChatService(
        retriever,
        scripted_chat_model(tool_calls=["zorblat"], answer_parts=ANSWER_PARTS),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    assert _names(events) == [
        "RunStartedEvent",
        "SourcesEvent",
        "AnswerDeltaEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    run_started = events[0]
    assert isinstance(run_started, RunStartedEvent)
    assert run_started.run_id
    assert run_started.mode == "hybrid"

    sources = events[1]
    assert isinstance(sources, SourcesEvent)
    assert sources.items[0].document_title == "Kotlin Notes"
    assert sources.items[0].content == "zorblat everywhere"

    # The tool forwarded the run's limit to the retriever.
    assert retriever.calls == [("zorblat", 8)]
    assert _answer_text(events) == "Zorblat is a test term used in fixtures [1]."

    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.outcome == "success"
    assert done.tool_calls == 1
    assert done.run_id == run_started.run_id
    assert done.latency_ms >= 0


async def test_each_tool_call_flushes_its_own_sources_event():
    retriever = _make_retriever()
    service = ChatService(
        retriever,
        scripted_chat_model(tool_calls=["zorblat", "quibnard"], answer_parts=ANSWER_PARTS),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    assert _names(events) == [
        "RunStartedEvent",
        "SourcesEvent",
        "SourcesEvent",
        "AnswerDeltaEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert retriever.calls == [("zorblat", 8), ("quibnard", 8)]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.tool_calls == 2


async def test_citation_numbers_continue_across_tool_calls():
    # Clients concatenate `sources` events, so a bracketed [1] after a second
    # retrieval would be ambiguous — numbering must stay unique for the run.
    tool_results: list[str] = []
    retriever = _make_retriever()
    service = ChatService(
        retriever,
        scripted_chat_model(
            tool_calls=["zorblat", "quibnard"],
            answer_parts=ANSWER_PARTS,
            tool_results=tool_results,
        ),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    assert len(tool_results) == 2
    assert tool_results[0].startswith("[1] ")
    assert tool_results[1].startswith("[2] ")  # continues, does not restart at [1]
    sources = [event for event in events if isinstance(event, SourcesEvent)]
    assert [len(event.items) for event in sources] == [1, 1]
    # Concatenated source order matches the citation numbers the model saw.
    assert tool_results[0].startswith("[1] Notes")
    assert tool_results[1].startswith("[2] Notes")


async def test_run_without_tool_calls_has_no_sources_event():
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(answer_parts=["I cannot answer."]),
        mode="bm25",
    )

    events = await _collect(service, QUESTION)

    assert _names(events) == [
        "RunStartedEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].mode == "bm25"
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].tool_calls == 0


async def test_empty_retrieval_still_emits_an_empty_sources_event():
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(tool_calls=["nothing-matches"], answer_parts=["No results."]),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    sources = [event for event in events if isinstance(event, SourcesEvent)]
    assert len(sources) == 1
    assert sources[0].items == []


async def test_provider_failure_mid_stream_ends_with_terminal_error_event():
    provider_error = openai.APIStatusError(
        "upstream exploded with secret detail",
        response=httpx.Response(500, request=httpx.Request("POST", "http://provider.test/v1/chat")),
        body=None,
    )
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(
            tool_calls=["zorblat"],
            answer_parts=["partial answer ", "never streamed"],
            fail_during_answer=provider_error,
            fail_after_parts=1,
        ),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    assert _names(events) == ["RunStartedEvent", "SourcesEvent", "AnswerDeltaEvent", "ErrorEvent"]
    assert _answer_text(events) == "partial answer "
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "llm_provider_error"
    # Provider internals stay out of the stream; they went to logs only.
    assert "upstream exploded" not in error.message
    assert not any(isinstance(event, DoneEvent) for event in events)


async def test_provider_rate_limit_maps_to_rate_limited_error_event():
    rate_limited = openai.RateLimitError(
        "quota exceeded",
        response=httpx.Response(429, request=httpx.Request("POST", "http://provider.test/v1/chat")),
        body=None,
    )
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(
            answer_parts=["never"],
            fail_during_answer=rate_limited,
            fail_after_parts=0,
        ),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "rate_limited"


async def test_model_http_error_maps_to_provider_error_event():
    """What the production model raises: pydantic-ai wraps provider HTTP
    failures in ModelHTTPError, which must reach the taxonomy (not the
    generic internal error) — regression for the FunctionModel-only blind spot."""
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(
            answer_parts=["never"],
            fail_during_answer=ModelHTTPError(status_code=503, model_name="failing", body=None),
            fail_after_parts=0,
        ),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "llm_provider_error"
    assert not any(isinstance(event, DoneEvent) for event in events)


async def test_model_http_429_maps_to_rate_limited_error_event():
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(
            answer_parts=["never"],
            fail_during_answer=ModelHTTPError(status_code=429, model_name="failing", body=None),
            fail_after_parts=0,
        ),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "rate_limited"


async def test_provider_failure_before_any_output_is_a_terminal_error_event():
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(
            answer_parts=["never"],
            fail_before_run=openai.APIConnectionError(
                request=httpx.Request("POST", "http://provider.test/v1/chat")
            ),
        ),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    assert _names(events) == ["RunStartedEvent", "ErrorEvent"]
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "llm_provider_error"


async def test_search_index_error_from_the_tool_is_never_answered_around():
    retriever = StubRetriever(
        error=SearchIndexError("index unavailable", details={"operation": "search_chunks"})
    )
    service = ChatService(
        retriever,
        scripted_chat_model(tool_calls=["zorblat"], answer_parts=["ungrounded answer"]),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    assert _names(events) == ["RunStartedEvent", "ErrorEvent"]
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "search_index_error"
    assert _answer_text(events) == ""  # never an ungrounded answer


async def test_unknown_failure_maps_to_generic_internal_error():
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(
            answer_parts=["partial"],
            fail_during_answer=RuntimeError("secret internal detail"),
            fail_after_parts=0,
        ),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "internal_error"
    assert error.message == "Internal server error"
    assert "secret internal detail" not in error.message


async def test_run_id_is_bound_into_every_log_line_and_question_text_never_logged():
    service = ChatService(
        _make_retriever(),
        scripted_chat_model(tool_calls=["zorblat"], answer_parts=ANSWER_PARTS),
        mode="hybrid",
    )

    # capture_logs replaces the processor chain, so merge_contextvars (which
    # carries the run_id binding into events) must be passed explicitly.
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        events = await _collect(service, QUESTION)

    run_id = events[0].run_id if isinstance(events[0], RunStartedEvent) else None
    assert run_id
    assert logs, "expected agent lifecycle logs"
    assert all(entry.get("run_id") == run_id for entry in logs)
    assert any(entry["event"] == "agent_run_started" for entry in logs)
    assert any(entry["event"] == "agent_run_finished" for entry in logs)
    started = next(entry for entry in logs if entry["event"] == "agent_run_started")
    assert started["question_length"] == len(QUESTION)
    # The question itself is user data: never logged at any level.
    assert QUESTION not in str(logs)


# --- external (MCP) tool integration (offline; fake manager) ---


def _mcp_tools(manager: FakeMcpManager) -> list[object]:
    """One wrapped external tool, as deps.py would build from a snapshot."""
    return build_agent_tools(
        manager,
        [
            McpToolInfo(
                server="alpha",
                name="add",
                description="Add two integers.",
                input_schema={
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                },
            )
        ],
    )


async def test_mcp_tool_call_streams_answer_and_counts_in_tool_calls() -> None:
    manager = FakeMcpManager(result=McpToolResult(text="3", structured={"result": 3}))
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(
            tool_calls=["add 1 and 2"],
            tool_name="mcp_alpha_add",
            tool_args=[{"a": 1, "b": 2}],
            answer_parts=["One plus two is 3 (external: alpha)."],
        ),
        mode="hybrid",
        extra_tools=_mcp_tools(manager),
    )

    events = await _collect(service, QUESTION)

    # No `sources` event: external tools never emit KB sources (by design).
    assert _names(events) == ["RunStartedEvent", "AnswerDeltaEvent", "DoneEvent"]
    assert _answer_text(events) == "One plus two is 3 (external: alpha)."
    assert manager.calls == [("alpha", "add", {"a": 1, "b": 2})]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.outcome == "success"
    assert done.tool_calls == 1  # the external call is counted


async def test_mcp_tool_failure_mid_run_still_ends_with_done() -> None:
    manager = FakeMcpManager(error=RuntimeError("transport exploded"))
    tool_results: list[str] = []
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(
            tool_calls=["add anything"],
            tool_name="mcp_alpha_add",
            tool_args=[{"a": 1, "b": 1}],
            answer_parts=["I could not verify that externally."],
            tool_results=tool_results,
        ),
        mode="hybrid",
        extra_tools=_mcp_tools(manager),
    )

    events = await _collect(service, QUESTION)

    # The degraded tool result reached the model; the run completed normally —
    # an external failure must never become a terminal error event.
    assert tool_results == ["tool mcp_alpha_add failed: RuntimeError"]
    assert _names(events) == ["RunStartedEvent", "AnswerDeltaEvent", "DoneEvent"]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.outcome == "success"
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_without_extra_tools_the_agent_behaves_as_before() -> None:
    # Default construction path (no MCP configured): identical contract to
    # the pre-MCP service, pinned next to the integration tests on purpose.
    retriever = _make_retriever()
    service = ChatService(
        retriever,
        scripted_chat_model(tool_calls=["zorblat"], answer_parts=ANSWER_PARTS),
        mode="hybrid",
    )

    events = await _collect(service, QUESTION)

    assert _names(events) == [
        "RunStartedEvent",
        "SourcesEvent",
        "AnswerDeltaEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]


async def test_collector_counts_retrieval_and_external_calls_together() -> None:
    # The run-wide `tool_calls` total mixes both tool families; pinned at the
    # collector level so the DoneEvent semantics stay explicit.
    collector = SourceCollector()

    collector.append([])
    collector.record_external_tool_call()
    collector.append([])

    assert collector.tool_calls == 3
    assert collector.total_hits == 0  # external calls contribute no KB hits


# --- session persistence (disposable test DB; scripted model throughout) ---


def _persisted_service(
    factory: async_sessionmaker[AsyncSession],
    model=None,
    *,
    budget: int = 8000,
) -> ChatService:
    """ChatService wired exactly like production: stub retriever, scripted
    model, one DB session per ask() via the injected factory."""
    return ChatService(
        StubRetriever(),
        model if model is not None else scripted_chat_model(answer_parts=list(ANSWER_PARTS)),
        mode="hybrid",
        session_factory=factory,
        history_char_budget=budget,
    )


async def _session_messages(
    factory: async_sessionmaker[AsyncSession], session_id: UUID
) -> list[ChatMessage]:
    async with factory() as session:
        return list(await ChatMessageRepository(session).list_for_session(session_id))


def _user_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


def _assistant_texts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, TextPart)
    ]


@pytest.mark.db
async def test_ask_without_session_id_creates_session_and_persists_the_turn(session_factory):
    service = _persisted_service(session_factory)

    events = await _collect(service, QUESTION)

    run_started = events[0]
    done = events[-1]
    assert isinstance(run_started, RunStartedEvent)
    assert isinstance(done, DoneEvent)
    assert run_started.session_id == done.session_id
    assert run_started.session_id is not None

    async with session_factory() as session:
        chat_session = await ChatSessionRepository(session).get_by_id(run_started.session_id)
    assert chat_session is not None
    assert chat_session.title == derive_title(QUESTION)

    messages = await _session_messages(session_factory, run_started.session_id)
    assert [(m.role, m.content) for m in messages] == [
        (MessageRole.USER, QUESTION),
        (MessageRole.ASSISTANT, "Zorblat is a test term used in fixtures [1]."),
    ]
    assert messages[0].run_id is None
    assert messages[1].run_id == UUID(hex=run_started.run_id)


@pytest.mark.db
async def test_second_turn_sees_first_turn_as_message_history(session_factory):
    first_histories: list[list[ModelMessage]] = []
    service1 = _persisted_service(
        session_factory,
        scripted_chat_model(answer_parts=["First answer."], histories=first_histories),
    )
    events = await _collect(service1, "first question")
    session_id = events[0].session_id
    assert session_id is not None

    second_histories: list[list[ModelMessage]] = []
    service2 = _persisted_service(
        session_factory,
        scripted_chat_model(answer_parts=["Second answer."], histories=second_histories),
    )
    events2 = await _collect(service2, "second question", session_id=session_id)
    assert events2[0].session_id == session_id

    assert second_histories, "the model must have been called"
    for messages in second_histories:
        # History precedes the current prompt: [q1, a1, q2].
        assert _user_prompts(messages)[:2] == ["first question", "second question"]
        assert _assistant_texts(messages) == ["First answer."]

    messages = await _session_messages(session_factory, session_id)
    assert [(m.role, m.content) for m in messages] == [
        (MessageRole.USER, "first question"),
        (MessageRole.ASSISTANT, "First answer."),
        (MessageRole.USER, "second question"),
        (MessageRole.ASSISTANT, "Second answer."),
    ]


@pytest.mark.db
async def test_failed_run_persists_user_message_only_and_session_continues(session_factory):
    provider_down = openai.APIConnectionError(
        request=httpx.Request("POST", "http://provider.test/v1/chat")
    )
    failing = _persisted_service(
        session_factory, scripted_chat_model(answer_parts=["never"], fail_before_run=provider_down)
    )
    events = await _collect(failing, "doomed question")

    assert [type(event).__name__ for event in events] == ["RunStartedEvent", "ErrorEvent"]
    session_id = events[0].session_id
    assert session_id is not None

    # The honest record: the user message survived, no assistant message.
    messages = await _session_messages(session_factory, session_id)
    assert [(m.role, m.content) for m in messages] == [(MessageRole.USER, "doomed question")]

    # And the session stays continuable; the unanswered question does not
    # leak into the next turn's history as an orphan half-turn.
    histories: list[list[ModelMessage]] = []
    recovered = _persisted_service(
        session_factory, scripted_chat_model(answer_parts=["Recovered."], histories=histories)
    )
    events2 = await _collect(recovered, "try again", session_id=session_id)
    assert isinstance(events2[-1], DoneEvent)
    assert all(_user_prompts(messages) == ["try again"] for messages in histories)
    messages = await _session_messages(session_factory, session_id)
    assert len(messages) == 3  # doomed user + retry user + recovered assistant


@pytest.mark.db
async def test_history_budget_drops_oldest_complete_turns(session_factory):
    # Each scripted turn costs exactly 4 chars ("u1"+"a1", "u2"+"a2").
    service1 = _persisted_service(session_factory, scripted_chat_model(answer_parts=["a1"]))
    events = await _collect(service1, "u1")
    session_id = events[0].session_id
    assert session_id is not None
    service2 = _persisted_service(session_factory, scripted_chat_model(answer_parts=["a2"]))
    await _collect(service2, "u2", session_id=session_id)

    # Budget 4 fits only the newest turn: turn 3 sees [u2, a2], never u1.
    histories: list[list[ModelMessage]] = []
    service3 = _persisted_service(
        session_factory,
        scripted_chat_model(answer_parts=["a3"], histories=histories),
        budget=4,
    )
    await _collect(service3, "u3", session_id=session_id)

    assert histories
    for messages in histories:
        assert _user_prompts(messages) == ["u2", "u3"]
        assert _assistant_texts(messages) == ["a2"]


@pytest.mark.db
async def test_history_read_limit_drops_older_turns_even_under_budget(session_factory, monkeypatch):
    # The 200-message read bound (HISTORY_READ_LIMIT) is a deliberate floor:
    # a session that outgrows it loses its oldest turns from history even
    # when the char budget would fit them — the read, not the budget, limits
    # first. Shrunk to 2 here so one session (two turns) crosses it.
    monkeypatch.setattr("app.services.chat.HISTORY_READ_LIMIT", 2)
    service1 = _persisted_service(session_factory, scripted_chat_model(answer_parts=["a1"]))
    events = await _collect(service1, "u1")
    session_id = events[0].session_id
    assert session_id is not None
    service2 = _persisted_service(session_factory, scripted_chat_model(answer_parts=["a2"]))
    await _collect(service2, "u2", session_id=session_id)

    # Default budget fits both turns; the read cap returns only the newest
    # two messages ([a2, u2]) — turn 1 was never read, so never assembled.
    histories: list[list[ModelMessage]] = []
    service3 = _persisted_service(
        session_factory, scripted_chat_model(answer_parts=["a3"], histories=histories)
    )
    await _collect(service3, "u3", session_id=session_id)

    assert histories
    for messages in histories:
        assert _user_prompts(messages) == ["u2", "u3"]
        assert _assistant_texts(messages) == ["a2"]


@pytest.mark.db
async def test_done_time_persist_failure_yields_terminal_error_and_user_only_record(
    session_factory, monkeypatch
):
    # Pins the persist-inside-the-try contract: a failure while storing the
    # assistant message at done time must surface as the terminal `error`
    # event (never escape the generator after streaming started) and leave
    # the honest user-only record.
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full at persist time")

    service = _persisted_service(session_factory)
    monkeypatch.setattr(service, "_persist_assistant_message", boom)
    events = await _collect(service, QUESTION)

    # The answer streamed first; the terminal event is the error — nothing
    # escaped the generator mid-stream.
    assert isinstance(events[0], RunStartedEvent)
    assert any(isinstance(event, AnswerDeltaEvent) for event in events)
    assert isinstance(events[-1], ErrorEvent)
    session_id = events[0].session_id
    assert session_id is not None
    messages = await _session_messages(session_factory, session_id)
    assert [(m.role, m.content) for m in messages] == [(MessageRole.USER, QUESTION)]


@pytest.mark.db
async def test_ask_with_unknown_session_id_raises_not_found_before_any_event(session_factory):
    service = _persisted_service(session_factory)

    with pytest.raises(NotFoundError):
        await _collect(service, QUESTION, session_id=uuid4())


@pytest.mark.db
async def test_ask_with_soft_deleted_session_id_raises_not_found(session_factory):
    service = _persisted_service(session_factory)
    events = await _collect(service, QUESTION)
    session_id = events[0].session_id
    assert session_id is not None

    async with session_factory() as session:
        repo = ChatSessionRepository(session)
        chat_session = await repo.get_by_id(session_id)
        assert chat_session is not None
        await repo.soft_delete(chat_session)
        await session.commit()

    with pytest.raises(NotFoundError):
        await _collect(service, QUESTION, session_id=session_id)


@pytest.mark.db
async def test_stateless_service_performs_no_db_writes(session_factory, db_engine):
    # The hard regression: a service constructed WITHOUT a session factory
    # (every pre-session construction, including the whole pre-existing
    # suite) never touches the chat tables.
    service = ChatService(StubRetriever(), scripted_chat_model(answer_parts=["ok"]), mode="bm25")

    events = await _collect(service, QUESTION)

    assert isinstance(events[0], RunStartedEvent)
    assert events[0].session_id is None
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].session_id is None
    async with db_engine.connect() as conn:
        sessions = (await conn.execute(text("SELECT count(*) FROM chat_sessions"))).scalar_one()
        messages = (await conn.execute(text("SELECT count(*) FROM chat_messages"))).scalar_one()
    assert (sessions, messages) == (0, 0)


# --- history-aware query rewriting (follow-up turns; scripted rewrite model) ---

TOPIC_QUESTION = "Redis 是什么?"
ANAPHORIC_FOLLOWUP = "那它的缺点呢?"
STANDALONE_QUERY = "Redis 的主要缺点有哪些?"


def _rewriting_service(
    factory: async_sessionmaker[AsyncSession],
    retriever: StubRetriever,
    rewrite_model,
    *,
    qa_model=None,
) -> ChatService:
    """ChatService wired with a rewrite model: scripted QA model, recording
    stub retriever, one DB session per ask() via the injected factory."""
    return ChatService(
        retriever,
        qa_model if qa_model is not None else scripted_chat_model(answer_parts=["Turn answer."]),
        mode="hybrid",
        session_factory=factory,
        rewrite_model=rewrite_model,
    )


@pytest.mark.db
async def test_followup_turn_retrieves_on_rewritten_standalone_query(session_factory):
    # AC1/AC6: turn 1 establishes the topic; the anaphoric turn-2 follow-up
    # ("那它的缺点呢?") must retrieve on the scripted standalone Chinese
    # query — never the raw referent-less text.
    rewrite_prompts: list[str] = []
    rewrite_histories: list[list[ModelMessage]] = []
    retriever = _make_retriever()
    rewriter = scripted_rewrite_model(
        [STANDALONE_QUERY], prompts=rewrite_prompts, histories=rewrite_histories
    )

    # Turn 1: topic-establishing question; no history — no rewrite, raw search.
    turn1 = _rewriting_service(
        session_factory,
        retriever,
        rewriter,
        qa_model=scripted_chat_model(tool_calls=["Redis 是什么"], answer_parts=["Turn answer."]),
    )
    events1 = await _collect(turn1, TOPIC_QUESTION)
    session_id = events1[0].session_id
    assert session_id is not None
    assert rewrite_prompts == []  # first turn: the rewriter never ran

    # Turn 2: anaphoric follow-up. The standalone rewrite becomes the run
    # prompt, and the QA model searches the distinctive terms of that
    # self-contained prompt (scripted here as the standalone query itself).
    qa_histories: list[list[ModelMessage]] = []
    turn2 = _rewriting_service(
        session_factory,
        retriever,
        rewriter,
        qa_model=scripted_chat_model(
            tool_calls=[STANDALONE_QUERY],
            answer_parts=["Turn answer."],
            histories=qa_histories,
        ),
    )
    await _collect(turn2, ANAPHORIC_FOLLOWUP, session_id=session_id)

    assert retriever.calls == [("Redis 是什么", 8), (STANDALONE_QUERY, 8)]
    # The rewriter ran exactly once, on the raw follow-up…
    assert rewrite_prompts == [ANAPHORIC_FOLLOWUP]
    # …and saw the prior turns as its history: [q1, a1] precede its prompt.
    assert len(rewrite_histories) == 1
    assert _user_prompts(rewrite_histories[0]) == [TOPIC_QUESTION, ANAPHORIC_FOLLOWUP]
    assert _assistant_texts(rewrite_histories[0]) == ["Turn answer."]
    # The QA run itself opened on the REWRITTEN prompt (the wiring under test):
    # its first request's user prompts are the original topic turn from
    # history, then the standalone rewrite as the run's own input.
    assert qa_histories
    assert _user_prompts(qa_histories[0]) == [TOPIC_QUESTION, STANDALONE_QUERY]


@pytest.mark.db
async def test_first_turn_performs_no_rewrite_call(session_factory):
    # AC2: with history empty the rewrite step is skipped entirely — no
    # rewrite call, and the raw question reaches retriever and agent unchanged.
    rewrite_prompts: list[str] = []
    retriever = _make_retriever()
    service = _rewriting_service(
        session_factory,
        retriever,
        scripted_rewrite_model([STANDALONE_QUERY], prompts=rewrite_prompts),
        qa_model=scripted_chat_model(
            tool_calls=[ANAPHORIC_FOLLOWUP], answer_parts=["Turn answer."]
        ),
    )

    events = await _collect(service, ANAPHORIC_FOLLOWUP)

    assert isinstance(events[-1], DoneEvent)
    assert rewrite_prompts == []
    assert retriever.calls == [(ANAPHORIC_FOLLOWUP, 8)]


async def test_stateless_service_never_rewrites():
    # AC2: without a session factory there is never history, hence never a
    # rewrite — the stateless behavior is byte-identical to the pre-rewrite
    # service even when a rewrite model is wired.
    rewrite_prompts: list[str] = []
    retriever = _make_retriever()
    service = ChatService(
        retriever,
        scripted_chat_model(tool_calls=[ANAPHORIC_FOLLOWUP], answer_parts=["Answer."]),
        mode="hybrid",
        rewrite_model=scripted_rewrite_model([STANDALONE_QUERY], prompts=rewrite_prompts),
    )

    events = await _collect(service, ANAPHORIC_FOLLOWUP)

    assert isinstance(events[-1], DoneEvent)
    assert rewrite_prompts == []
    assert retriever.calls == [(ANAPHORIC_FOLLOWUP, 8)]


@pytest.mark.db
async def test_rewrite_keeps_original_question_persisted_and_in_history(session_factory):
    # AC3/R3: the rewritten query only ever replaces the run prompt — the
    # persisted user message and later turns' message_history keep the
    # original text.
    retriever = _make_retriever()
    service = _rewriting_service(
        session_factory, retriever, scripted_rewrite_model([STANDALONE_QUERY])
    )
    events1 = await _collect(service, TOPIC_QUESTION)
    session_id = events1[0].session_id
    assert session_id is not None
    await _collect(service, ANAPHORIC_FOLLOWUP, session_id=session_id)

    messages = await _session_messages(session_factory, session_id)
    assert [(m.role, m.content) for m in messages] == [
        (MessageRole.USER, TOPIC_QUESTION),
        (MessageRole.ASSISTANT, "Turn answer."),
        (MessageRole.USER, ANAPHORIC_FOLLOWUP),  # original, not the rewrite
        (MessageRole.ASSISTANT, "Turn answer."),
    ]

    # A following (non-rewriting) turn's message_history carries the
    # originals: neither history user entry is the rewritten form.
    histories: list[list[ModelMessage]] = []
    plain = ChatService(
        _make_retriever(),
        scripted_chat_model(answer_parts=["Third."], histories=histories),
        mode="hybrid",
        session_factory=session_factory,
    )
    await _collect(plain, "third question", session_id=session_id)

    assert histories
    for messages in histories:
        assert _user_prompts(messages) == [
            TOPIC_QUESTION,
            ANAPHORIC_FOLLOWUP,
            "third question",
        ]


@pytest.mark.db
async def test_rewrite_disabled_construction_passes_raw_questions(session_factory):
    # AC4: without a rewrite model (the default construction and the
    # Settings-disabled wiring) every turn passes the raw question through.
    qa_histories: list[list[ModelMessage]] = []
    retriever = _make_retriever()
    turn1 = ChatService(
        retriever,
        scripted_chat_model(
            tool_calls=[TOPIC_QUESTION], answer_parts=["Turn answer."], histories=qa_histories
        ),
        mode="hybrid",
        session_factory=session_factory,
    )
    events1 = await _collect(turn1, TOPIC_QUESTION)
    session_id = events1[0].session_id
    assert session_id is not None

    turn2 = ChatService(
        retriever,
        scripted_chat_model(
            tool_calls=[ANAPHORIC_FOLLOWUP],
            answer_parts=["Turn answer."],
            histories=qa_histories,
        ),
        mode="hybrid",
        session_factory=session_factory,
    )
    events2 = await _collect(turn2, ANAPHORIC_FOLLOWUP, session_id=session_id)

    assert isinstance(events2[-1], DoneEvent)
    # The retriever saw the raw questions: an anaphoric follow-up retrieves
    # on its verbatim text — exactly the pre-rewrite behavior.
    assert retriever.calls == [(TOPIC_QUESTION, 8), (ANAPHORIC_FOLLOWUP, 8)]
    # Both turns' model requests ran on the raw questions as their prompts
    # (two requests per turn: tool call, then answer).
    assert [_user_prompts(messages) for messages in qa_histories] == [
        [TOPIC_QUESTION],
        [TOPIC_QUESTION],
        [TOPIC_QUESTION, ANAPHORIC_FOLLOWUP],
        [TOPIC_QUESTION, ANAPHORIC_FOLLOWUP],
    ]


@pytest.mark.db
async def test_rewrite_failure_degrades_to_raw_query_and_still_completes(session_factory):
    # AC5: a rewrite provider failure degrades to the raw question and the
    # answer still streams to a normal done — the rewrite step never turns a
    # would-be answer into a terminal error.
    rewriter = scripted_rewrite_model(
        [STANDALONE_QUERY],
        fail=openai.APIConnectionError(
            request=httpx.Request("POST", "http://provider.test/v1/chat")
        ),
    )
    retriever = _make_retriever()

    # Turn 1: no history, so the failing rewriter is never invoked.
    turn1 = _rewriting_service(
        session_factory,
        retriever,
        rewriter,
        qa_model=scripted_chat_model(tool_calls=[TOPIC_QUESTION], answer_parts=["Turn answer."]),
    )
    events1 = await _collect(turn1, TOPIC_QUESTION)
    session_id = events1[0].session_id
    assert session_id is not None
    assert isinstance(events1[-1], DoneEvent)

    # Turn 2: the rewrite call fails; the run continues on the raw question.
    turn2 = _rewriting_service(
        session_factory,
        retriever,
        rewriter,
        qa_model=scripted_chat_model(
            tool_calls=[ANAPHORIC_FOLLOWUP], answer_parts=["Turn answer."]
        ),
    )
    with capture_logs() as logs:
        events2 = await _collect(turn2, ANAPHORIC_FOLLOWUP, session_id=session_id)

    # The run completed normally, retrieving on the raw question.
    assert _names(events2) == [
        "RunStartedEvent",
        "SourcesEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    done = events2[-1]
    assert isinstance(done, DoneEvent)
    assert done.outcome == "success"
    assert not any(isinstance(event, ErrorEvent) for event in events2)
    assert retriever.calls == [(TOPIC_QUESTION, 8), (ANAPHORIC_FOLLOWUP, 8)]
    # The failure was logged once, error class only — no question text.
    failed = [entry for entry in logs if entry["event"] == "query_rewrite_failed"]
    assert len(failed) == 1
    assert failed[0]["error_class"] == "APIConnectionError"
    assert ANAPHORIC_FOLLOWUP not in str(logs)


@pytest.mark.db
async def test_rewrite_logs_flags_and_lengths_only(session_factory):
    # AC7: the rewrite observability event carries flags and lengths — never
    # the question or the rewritten query text.
    rewriter = scripted_rewrite_model([STANDALONE_QUERY])
    service = _rewriting_service(session_factory, _make_retriever(), rewriter)

    events1 = await _collect(service, TOPIC_QUESTION)
    session_id = events1[0].session_id
    assert session_id is not None
    with capture_logs() as logs:
        await _collect(service, ANAPHORIC_FOLLOWUP, session_id=session_id)

    rewrites = [entry for entry in logs if entry["event"] == "query_rewrite"]
    assert len(rewrites) == 1
    entry = rewrites[0]
    assert entry["applied"] is True
    assert entry["changed"] is True
    assert entry["original_length"] == len(ANAPHORIC_FOLLOWUP)
    assert entry["rewritten_length"] == len(STANDALONE_QUERY)
    # User data stays out of logs: neither text appears anywhere.
    assert ANAPHORIC_FOLLOWUP not in str(logs)
    assert STANDALONE_QUERY not in str(logs)
