"""Chat service stream semantics: event order, sources, failures, sessions.

The model is always a scripted FunctionModel and the retriever a stub — no
live LLM, no infrastructure. Provider failures are scripted with the openai
SDK's own exception types so the service's error mapping is exercised for
real. Session-persistence tests run against the disposable test database
(`db`-marked) with the service wired to the per-test session factory — the
production lifetime pattern.
"""

from __future__ import annotations

from collections.abc import Callable
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
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.tenant import DEFAULT_TENANT_ID
from app.rag.retriever import RetrievedChunk, SearchOutcome
from app.repositories.chat import ChatMessageRepository, ChatSessionRepository
from app.schemas.chat import (
    AnswerDeltaEvent,
    DoneEvent,
    ErrorEvent,
    QueryRewrittenEvent,
    RunStartedEvent,
    SourcesEvent,
    StatusEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from app.schemas.search import SearchHit
from app.services.chat import (
    CARRIED_SOURCES_LABEL,
    SUMMARY_PREFIX_LABEL,
    TRUNCATION_MARKER,
    ChatService,
    carried_prefix,
)
from app.services.session import derive_title
from fakes import (
    FakeMcpManager,
    StubRetriever,
    hermetic_settings,
    retrieved_chunk,
    scripted_chat_model,
    scripted_rewrite_model,
    scripted_summary_model,
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
    return [
        event
        async for event in service.ask(
            question, limit=limit, session_id=session_id, tenant_id=DEFAULT_TENANT_ID
        )
    ]


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
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    run_started = events[0]
    assert isinstance(run_started, RunStartedEvent)
    assert run_started.run_id
    assert run_started.mode == "hybrid"

    started = events[1]
    assert isinstance(started, ToolCallStartedEvent)
    # Started args carry the model's query plus the run's limit from deps.
    assert started.call_id
    assert started.tool_name == "search_knowledge"
    assert started.args == {"query": "zorblat", "limit": 8}

    sources = events[2]
    assert isinstance(sources, SourcesEvent)
    assert sources.items[0].document_title == "Kotlin Notes"
    assert sources.items[0].content == "zorblat everywhere"

    finished = events[3]
    assert isinstance(finished, ToolCallFinishedEvent)
    assert finished.call_id == started.call_id
    assert finished.tool_name == "search_knowledge"
    assert finished.status == "success"
    assert finished.latency_ms >= 0

    # `status(generating)` marks the first answer delta, exactly once.
    status = events[4]
    assert isinstance(status, StatusEvent)
    assert status.phase == "generating"
    assert [e for e in events if isinstance(e, StatusEvent)] == [status]

    # The tool forwarded the run's limit to the retriever.
    assert retriever.calls == [("zorblat", 8, DEFAULT_TENANT_ID)]
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
        "ToolCallStartedEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert retriever.calls == [
        ("zorblat", 8, DEFAULT_TENANT_ID),
        ("quibnard", 8, DEFAULT_TENANT_ID),
    ]
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
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].mode == "bm25"
    # No tool work happened; `generating` is still the pre-answer signal.
    assert isinstance(events[1], StatusEvent)
    assert events[1].phase == "generating"
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

    assert _names(events) == [
        "RunStartedEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "ErrorEvent",
    ]
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
    assert _names(events) == [
        "RunStartedEvent",
        "ToolCallStartedEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    # Progress events carry the MCP kwargs the model sent, untouched.
    started = next(e for e in events if isinstance(e, ToolCallStartedEvent))
    assert started.tool_name == "mcp_alpha_add"
    assert started.args == {"a": 1, "b": 2}
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
    assert _names(events) == [
        "RunStartedEvent",
        "ToolCallStartedEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    # The soft failure is visible as progress (status: failed), not an error.
    finished = next(e for e in events if isinstance(e, ToolCallFinishedEvent))
    assert finished.status == "failed"
    assert not any(isinstance(event, ErrorEvent) for event in events)
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
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
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
    budget: int = 2000,
    fraction: float = 0.5,
    token_counter: Callable[[str], int] = len,
) -> ChatService:
    """ChatService wired exactly like production: stub retriever, scripted
    model, one DB session per ask() via the injected factory. The counter is
    injected (default `len`) so budget behavior is deterministic and the
    suite never constructs a real tokenizer; `fraction >= 1.0` disables the
    per-turn guardrail for all-or-nothing budget tests."""
    return ChatService(
        StubRetriever(),
        model if model is not None else scripted_chat_model(answer_parts=list(ANSWER_PARTS)),
        mode="hybrid",
        session_factory=factory,
        history_token_budget=budget,
        history_max_turn_fraction=fraction,
        token_counter=token_counter,
    )


async def _session_messages(
    factory: async_sessionmaker[AsyncSession], session_id: UUID
) -> list[ChatMessage]:
    async with factory() as session:
        return list(
            await ChatMessageRepository(session).list_for_session(
                session_id, tenant_id=DEFAULT_TENANT_ID
            )
        )


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
        chat_session = await ChatSessionRepository(session).get_by_id(
            run_started.session_id, tenant_id=DEFAULT_TENANT_ID
        )
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
    # Each scripted turn costs exactly 4 tokens under the `len` counter
    # ("u1"+"a1", "u2"+"a2"); fraction=1.0 disables the guardrail so the
    # all-or-nothing budget walk is what is under test.
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
        fraction=1.0,
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
async def test_token_measure_drops_oldest_turn_that_char_budget_would_keep(session_factory):
    # AC1: under the injected word-counting counter the older turn no longer
    # fits the token budget, although its characters would — the injected
    # measure, not len(), decides what reaches the model.
    async def seed_session() -> UUID:
        service1 = _persisted_service(
            session_factory,
            scripted_chat_model(answer_parts=["seven eight nine ten"]),
        )
        events = await _collect(service1, "one two three four five six")
        session_id = events[0].session_id
        assert session_id is not None
        service2 = _persisted_service(session_factory, scripted_chat_model(answer_parts=["no"]))
        await _collect(service2, "yes", session_id=session_id)
        return session_id

    def _word_count(text: str) -> int:
        return len(text.split())

    # Session A, word counter (budget 10): the newest turn costs 2 tokens
    # ("yes"+"no") and the older one 10 ("one two... six" + "seven...ten") —
    # only the newest turn fits beside it.
    session_a = await seed_session()
    histories_a: list[list[ModelMessage]] = []
    word_counter = _persisted_service(
        session_factory,
        scripted_chat_model(answer_parts=["a3"], histories=histories_a),
        budget=10,
        fraction=1.0,
        token_counter=_word_count,
    )
    await _collect(word_counter, "u3", session_id=session_a)
    assert histories_a
    for messages in histories_a:
        assert _user_prompts(messages) == ["yes", "u3"]
        assert _assistant_texts(messages) == ["no"]

    # Session B, same turns under the `len` counter (budget 60 — the char
    # proxy at a comparable operator scale): both turns fit, proving the
    # budget followed the injected measure, not chars.
    session_b = await seed_session()
    histories_b: list[list[ModelMessage]] = []
    len_counter = _persisted_service(
        session_factory,
        scripted_chat_model(answer_parts=["a3"], histories=histories_b),
        budget=60,
        fraction=1.0,
    )
    await _collect(len_counter, "u3", session_id=session_b)
    assert histories_b
    for messages in histories_b:
        assert _user_prompts(messages) == ["one two three four five six", "yes", "u3"]
        assert _assistant_texts(messages) == ["seven eight nine ten", "no"]


@pytest.mark.db
async def test_oversized_turn_truncated_in_history_and_persisted_full(session_factory):
    # AC2/AC3: a long pasted-document turn is admitted truncated-with-marker
    # and an older turn survives beside it (no silent single-turn collapse),
    # while the stored rows keep FULL content — bounding only shapes the
    # assembled history copy, never the persistence record.
    service1 = _persisted_service(session_factory, scripted_chat_model(answer_parts=["a1"]))
    events = await _collect(service1, "u1")
    session_id = events[0].session_id
    assert session_id is not None

    long_document = "word " * 200  # 1000 chars, far over the per-turn cap below
    async with session_factory() as session:
        repo = ChatMessageRepository(session)
        await repo.add(
            ChatMessage(session_id=session_id, role=MessageRole.USER, content=long_document)
        )
        await repo.add(ChatMessage(session_id=session_id, role=MessageRole.ASSISTANT, content="a2"))
        await session.commit()

    # Budget 200, default fraction 0.5 -> per-turn cap 100 under `len`.
    histories: list[list[ModelMessage]] = []
    bounded_service = _persisted_service(
        session_factory,
        scripted_chat_model(answer_parts=["a3"], histories=histories),
        budget=200,
    )
    await _collect(bounded_service, "u3", session_id=session_id)

    assert histories
    for messages in histories:
        prompts = _user_prompts(messages)
        assert prompts[0] == "u1"  # the older turn survived the oversized one
        bounded = prompts[1]
        assert bounded.endswith(TRUNCATION_MARKER)
        assert bounded.startswith("word word ")  # a prefix of the document
        assert len(bounded) < len(long_document)
        assert _assistant_texts(messages) == ["a1", "a2"]
        assert prompts[-1] == "u3"

    rows = await _session_messages(session_factory, session_id)
    assert long_document in [message.content for message in rows]
    assert not any(TRUNCATION_MARKER in message.content for message in rows)


@pytest.mark.db
async def test_guardrail_disabled_drops_oversized_turn_whole(session_factory):
    # fraction >= 1.0 disables the guardrail: the oversized newest turn stops
    # the walk whole (the char-budget-era all-or-nothing behavior) — no
    # marker, no truncation, and no older turn beyond it either.
    service1 = _persisted_service(session_factory, scripted_chat_model(answer_parts=["a1"]))
    events = await _collect(service1, "u1")
    session_id = events[0].session_id
    assert session_id is not None

    long_document = "word " * 200
    async with session_factory() as session:
        repo = ChatMessageRepository(session)
        await repo.add(
            ChatMessage(session_id=session_id, role=MessageRole.USER, content=long_document)
        )
        await repo.add(ChatMessage(session_id=session_id, role=MessageRole.ASSISTANT, content="a2"))
        await session.commit()

    histories: list[list[ModelMessage]] = []
    disabled = _persisted_service(
        session_factory,
        scripted_chat_model(answer_parts=["a3"], histories=histories),
        budget=200,
        fraction=1.0,
    )
    await _collect(disabled, "u3", session_id=session_id)

    assert histories
    for messages in histories:
        # The oversized turn + everything older were dropped whole.
        assert _user_prompts(messages) == ["u3"]
        assert _assistant_texts(messages) == []


def test_history_budget_settings_defaults_are_pinned():
    # The token-budget migration (breaking rename of CHAT_HISTORY_CHAR_BUDGET):
    # defaults stay pinned so a silent change of the effective window is a
    # reviewed event, not a surprise.
    settings = hermetic_settings()
    assert settings.CHAT_HISTORY_TOKEN_BUDGET == 2000
    assert settings.CHAT_HISTORY_MAX_TURN_FRACTION == 0.5


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
        chat_session = await repo.get_by_id(session_id, tenant_id=DEFAULT_TENANT_ID)
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
    stub retriever, one DB session per ask() via the injected factory. The
    `len` counter keeps history assembly offline (no tokenizer built)."""
    return ChatService(
        retriever,
        qa_model if qa_model is not None else scripted_chat_model(answer_parts=["Turn answer."]),
        mode="hybrid",
        session_factory=factory,
        token_counter=len,
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

    assert retriever.calls == [
        ("Redis 是什么", 8, DEFAULT_TENANT_ID),
        (STANDALONE_QUERY, 8, DEFAULT_TENANT_ID),
    ]
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
async def test_followup_turn_emits_full_progress_order_with_query_rewritten(session_factory):
    # AC1: canonical follow-up order — run_started → status(rewriting_query)
    # → query_rewritten → tool_call_started → sources → tool_call_finished →
    # status(generating) → answer_delta* → done, with the rewrite event
    # carrying original/rewritten/applied/changed.
    retriever = _make_retriever()
    rewriter = scripted_rewrite_model([STANDALONE_QUERY])

    turn1 = _rewriting_service(
        session_factory,
        retriever,
        rewriter,
        qa_model=scripted_chat_model(tool_calls=[TOPIC_QUESTION], answer_parts=["Turn answer."]),
    )
    events1 = await _collect(turn1, TOPIC_QUESTION)
    session_id = events1[0].session_id
    assert session_id is not None

    turn2 = _rewriting_service(
        session_factory,
        retriever,
        rewriter,
        qa_model=scripted_chat_model(tool_calls=[STANDALONE_QUERY], answer_parts=["Turn answer."]),
    )
    events2 = await _collect(turn2, ANAPHORIC_FOLLOWUP, session_id=session_id)

    assert _names(events2) == [
        "RunStartedEvent",
        "StatusEvent",
        "QueryRewrittenEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    phases = [e.phase for e in events2 if isinstance(e, StatusEvent)]
    assert phases == ["rewriting_query", "generating"]
    rewritten = events2[2]
    assert isinstance(rewritten, QueryRewrittenEvent)
    assert rewritten.original == ANAPHORIC_FOLLOWUP
    assert rewritten.rewritten == STANDALONE_QUERY
    assert rewritten.applied is True
    assert rewritten.changed is True
    # The started event's query is the model's own (rewritten) retrieval query.
    started = next(e for e in events2 if isinstance(e, ToolCallStartedEvent))
    assert started.args["query"] == STANDALONE_QUERY


@pytest.mark.db
async def test_first_turn_emits_no_rewrite_events_but_tool_progress(session_factory):
    # AC2: first turn with a rewrite agent wired — no rewriting_query status
    # and no query_rewritten, but tool-call progress events still flow.
    rewriter = scripted_rewrite_model([STANDALONE_QUERY])
    service = _rewriting_service(
        session_factory,
        _make_retriever(),
        rewriter,
        qa_model=scripted_chat_model(
            tool_calls=[ANAPHORIC_FOLLOWUP], answer_parts=["Turn answer."]
        ),
    )

    events = await _collect(service, ANAPHORIC_FOLLOWUP)

    assert _names(events) == [
        "RunStartedEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert not any(isinstance(event, QueryRewrittenEvent) for event in events)
    # The single status is the pre-answer `generating` phase.
    assert [e.phase for e in events if isinstance(e, StatusEvent)] == ["generating"]


@pytest.mark.db
async def test_unchanged_rewrite_emits_no_query_rewritten(session_factory):
    # A rewrite returning the original text unchanged is silent on the wire
    # (the event gate requires `changed`), and the run completes normally.
    rewriter = scripted_rewrite_model([ANAPHORIC_FOLLOWUP])
    turn1 = _rewriting_service(
        session_factory,
        _make_retriever(),
        rewriter,
        qa_model=scripted_chat_model(tool_calls=["x"], answer_parts=["Turn answer."]),
    )
    events1 = await _collect(turn1, TOPIC_QUESTION)
    session_id = events1[0].session_id
    assert session_id is not None

    turn2 = _rewriting_service(
        session_factory,
        _make_retriever(),
        rewriter,
        qa_model=scripted_chat_model(
            tool_calls=[ANAPHORIC_FOLLOWUP], answer_parts=["Turn answer."]
        ),
    )
    events2 = await _collect(turn2, ANAPHORIC_FOLLOWUP, session_id=session_id)

    assert not any(isinstance(event, QueryRewrittenEvent) for event in events2)
    assert isinstance(events2[-1], DoneEvent)


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
    assert retriever.calls == [(ANAPHORIC_FOLLOWUP, 8, DEFAULT_TENANT_ID)]


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
    assert retriever.calls == [(ANAPHORIC_FOLLOWUP, 8, DEFAULT_TENANT_ID)]


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
        token_counter=len,
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
        token_counter=len,
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
        token_counter=len,
    )
    events2 = await _collect(turn2, ANAPHORIC_FOLLOWUP, session_id=session_id)

    assert isinstance(events2[-1], DoneEvent)
    # The retriever saw the raw questions: an anaphoric follow-up retrieves
    # on its verbatim text — exactly the pre-rewrite behavior.
    assert retriever.calls == [
        (TOPIC_QUESTION, 8, DEFAULT_TENANT_ID),
        (ANAPHORIC_FOLLOWUP, 8, DEFAULT_TENANT_ID),
    ]
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

    # The run completed normally, retrieving on the raw question. The silent
    # rewrite attempt is visible (`status(rewriting_query)`) but a degraded
    # rewrite never emits `query_rewritten` (AC3).
    assert _names(events2) == [
        "RunStartedEvent",
        "StatusEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert not any(isinstance(event, QueryRewrittenEvent) for event in events2)
    statuses = [e for e in events2 if isinstance(e, StatusEvent)]
    assert [status.phase for status in statuses] == ["rewriting_query", "generating"]
    done = events2[-1]
    assert isinstance(done, DoneEvent)
    assert done.outcome == "success"
    assert not any(isinstance(event, ErrorEvent) for event in events2)
    assert retriever.calls == [
        (TOPIC_QUESTION, 8, DEFAULT_TENANT_ID),
        (ANAPHORIC_FOLLOWUP, 8, DEFAULT_TENANT_ID),
    ]
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


# --- rolling summary of evicted history (scripted summary model; disposable DB) ---
#
# Arithmetic under the `len` counter with the guardrail off (fraction 1.0):
# every "uN"/"aN" turn costs exactly 4 tokens. A budget of 10 keeps two turns
# in the window, so turn 3's post-answer maintenance evicts and folds turn 1;
# the short scripted summaries ("S1", 2 tokens) reserve 2 of the 10, leaving 8
# — still two turns — so the next prelude shows [summary, u2, a2, u3, a3].


def _summarizing_service(
    factory: async_sessionmaker[AsyncSession],
    summary_model,
    *,
    answer: str,
    budget: int = 10,
    histories: list[list[ModelMessage]] | None = None,
) -> ChatService:
    """ChatService wired with a summary model: scripted QA model, stub
    retriever, one DB session per ask() via the injected factory, `len`
    counter, guardrail off — the production shape minus the LLM."""
    return ChatService(
        StubRetriever(),
        scripted_chat_model(answer_parts=[answer], histories=histories),
        mode="hybrid",
        session_factory=factory,
        history_token_budget=budget,
        history_max_turn_fraction=1.0,
        token_counter=len,
        summary_model=summary_model,
    )


async def _session_row(factory: async_sessionmaker[AsyncSession], session_id: UUID) -> ChatSession:
    async with factory() as session:
        row = await ChatSessionRepository(session).get_by_id(
            session_id, tenant_id=DEFAULT_TENANT_ID
        )
    assert row is not None
    return row


async def _seed_folded_session(factory: async_sessionmaker[AsyncSession], summary_model) -> UUID:
    """Three turns (u1..u3) under the 10-token budget: turn 3's maintenance
    evicts turn 1 and folds it, so the session leaves here with a summary
    whose watermark is a1."""
    events = await _collect(_summarizing_service(factory, summary_model, answer="a1"), "u1")
    session_id = events[0].session_id
    assert session_id is not None
    for question, answer in (("u2", "a2"), ("u3", "a3")):
        await _collect(
            _summarizing_service(factory, summary_model, answer=answer),
            question,
            session_id=session_id,
        )
    return session_id


@pytest.mark.db
async def test_evicted_turn_is_folded_into_the_stored_rolling_summary(session_factory):
    # AC1: once the oldest turn falls outside the token window its content is
    # represented in the session's stored summary, and the raw turn is no
    # longer in the in-window history the model receives.
    fold_prompts: list[str] = []
    summarizer = scripted_summary_model(["S1", "S2"], prompts=fold_prompts)

    events = await _collect(_summarizing_service(session_factory, summarizer, answer="a1"), "u1")
    session_id = events[0].session_id
    assert session_id is not None
    await _collect(
        _summarizing_service(session_factory, summarizer, answer="a2"), "u2", session_id=session_id
    )
    # Two turns (8 tokens) fit the budget of 10: nothing evicted, nothing folded.
    assert fold_prompts == []
    row = await _session_row(session_factory, session_id)
    assert (row.rolling_summary, row.summarized_through_id) == (None, None)

    # Turn 3 pushes turn 1 out of the window; the fold runs after the answer.
    events3 = await _collect(
        _summarizing_service(session_factory, summarizer, answer="a3"), "u3", session_id=session_id
    )
    assert isinstance(events3[-1], DoneEvent)
    assert len(fold_prompts) == 1
    assert "User: u1\nAssistant: a1" in fold_prompts[0]
    assert "u2" not in fold_prompts[0]
    assert "u3" not in fold_prompts[0]
    row = await _session_row(session_factory, session_id)
    assert row.rolling_summary == "S1"
    messages = await _session_messages(session_factory, session_id)
    assert row.summarized_through_id == messages[1].id  # a1: the newest folded message

    # Turn 4: the raw turn 1 is gone from the in-window history — its memory
    # arrives through the leading summary instead.
    histories: list[list[ModelMessage]] = []
    await _collect(
        _summarizing_service(session_factory, summarizer, answer="a4", histories=histories),
        "u4",
        session_id=session_id,
    )
    assert histories
    for seen in histories:
        prompts = _user_prompts(seen)
        assert "u1" not in prompts
        assert prompts[0].startswith(SUMMARY_PREFIX_LABEL)
        assert "a1" not in _assistant_texts(seen)


@pytest.mark.db
async def test_rolling_summary_leads_the_next_turn_history_as_labeled_context(session_factory):
    # AC2: the summary is injected AHEAD of the in-window turns as a labeled
    # synthetic exchange (request + acknowledgement), never as a bare user turn.
    summarizer = scripted_summary_model(["S1"])
    session_id = await _seed_folded_session(session_factory, summarizer)

    histories: list[list[ModelMessage]] = []
    await _collect(
        _summarizing_service(session_factory, summarizer, answer="a4", histories=histories),
        "u4",
        session_id=session_id,
    )

    assert histories
    for seen in histories:
        assert _user_prompts(seen) == [f"{SUMMARY_PREFIX_LABEL}\nS1", "u2", "u3", "u4"]
        answers = _assistant_texts(seen)
        assert answers[1:] == ["a2", "a3"]
        assert answers[0]  # the synthetic acknowledgement keeps alternation clean
        assert isinstance(seen[0], ModelRequest)
        assert isinstance(seen[1], ModelResponse)


@pytest.mark.db
async def test_folding_is_incremental_and_runs_only_when_new_turns_evict(session_factory):
    # AC3: a turn already folded is never re-summarized — the watermark
    # advances and each fold carries the existing summary plus the newly
    # evicted turns only; a turn that evicts nothing makes no fold call.
    fold_prompts: list[str] = []
    summarizer = scripted_summary_model(["S1", "S2"], prompts=fold_prompts)
    session_id = await _seed_folded_session(session_factory, summarizer)
    assert len(fold_prompts) == 1

    # Turn 4 evicts turn 2: the fold sees summary S1 + turn 2, never turn 1 again.
    await _collect(
        _summarizing_service(session_factory, summarizer, answer="a4"), "u4", session_id=session_id
    )
    assert len(fold_prompts) == 2
    assert "# Existing summary\nS1\n" in fold_prompts[1]
    assert "User: u2\nAssistant: a2" in fold_prompts[1]
    assert "User: u1" not in fold_prompts[1]
    row = await _session_row(session_factory, session_id)
    messages = await _session_messages(session_factory, session_id)
    assert row.rolling_summary == "S2"
    assert row.summarized_through_id == messages[3].id  # a2

    # A wide budget evicts nothing new: no fold call, summary/watermark untouched.
    await _collect(
        _summarizing_service(session_factory, summarizer, answer="a5", budget=1000),
        "u5",
        session_id=session_id,
    )
    assert len(fold_prompts) == 2
    row = await _session_row(session_factory, session_id)
    assert row.rolling_summary == "S2"
    assert row.summarized_through_id == messages[3].id


@pytest.mark.db
async def test_rolling_summary_disabled_is_plain_cliff_eviction(session_factory):
    # AC4: without a summary model (the default construction and the
    # Settings-disabled wiring) nothing is read, folded, or injected — the
    # oldest turn simply vanishes at the window edge, exactly as before.
    def disabled(answer: str, histories: list[list[ModelMessage]] | None = None) -> ChatService:
        return ChatService(
            StubRetriever(),
            scripted_chat_model(answer_parts=[answer], histories=histories),
            mode="hybrid",
            session_factory=session_factory,
            history_token_budget=10,
            history_max_turn_fraction=1.0,
            token_counter=len,
        )

    events = await _collect(disabled("a1"), "u1")
    session_id = events[0].session_id
    assert session_id is not None
    await _collect(disabled("a2"), "u2", session_id=session_id)
    histories: list[list[ModelMessage]] = []
    with capture_logs() as logs:
        await _collect(disabled("a3"), "u3", session_id=session_id)
        await _collect(disabled("a4", histories), "u4", session_id=session_id)

    assert histories
    for seen in histories:
        assert _user_prompts(seen) == ["u2", "u3", "u4"]  # the cliff: u1 is gone, no prefix
        assert _assistant_texts(seen) == ["a2", "a3"]
    row = await _session_row(session_factory, session_id)
    assert (row.rolling_summary, row.summarized_through_id) == (None, None)
    assert not any(entry["event"].startswith("rolling_summary") for entry in logs)


@pytest.mark.db
async def test_summary_fold_failure_keeps_the_turn_successful_and_retries_later(session_factory):
    # AC5: a scripted fold failure never touches the answer stream (the turn
    # still reaches `done`, no `error` event), leaves summary/watermark
    # unchanged, and the unfolded turns are folded on a later turn.
    failed_prompts: list[str] = []
    failing = scripted_summary_model(
        ["never"],
        prompts=failed_prompts,
        fail=openai.APIConnectionError(
            request=httpx.Request("POST", "http://provider.test/v1/chat")
        ),
    )
    events = await _collect(_summarizing_service(session_factory, failing, answer="a1"), "u1")
    session_id = events[0].session_id
    assert session_id is not None
    await _collect(
        _summarizing_service(session_factory, failing, answer="a2"), "u2", session_id=session_id
    )
    with capture_logs() as logs:
        events3 = await _collect(
            _summarizing_service(session_factory, failing, answer="a3"),
            "u3",
            session_id=session_id,
        )

    # The fold was attempted and failed; the turn completed normally regardless.
    assert len(failed_prompts) == 1
    assert _names(events3) == ["RunStartedEvent", "StatusEvent", "AnswerDeltaEvent", "DoneEvent"]
    done = events3[-1]
    assert isinstance(done, DoneEvent)
    assert done.outcome == "success"
    failed = [entry for entry in logs if entry["event"] == "rolling_summary_update_failed"]
    assert len(failed) == 1
    assert failed[0]["error_class"] == "APIConnectionError"
    row = await _session_row(session_factory, session_id)
    assert (row.rolling_summary, row.summarized_through_id) == (None, None)

    # Next turn with a working summarizer: by now turns 1 AND 2 are outside
    # the window, so both are folded — from an empty summary — in one pass.
    fold_prompts: list[str] = []
    working = scripted_summary_model(["S1"], prompts=fold_prompts)
    await _collect(
        _summarizing_service(session_factory, working, answer="a4"), "u4", session_id=session_id
    )
    assert len(fold_prompts) == 1
    assert "User: u1\nAssistant: a1" in fold_prompts[0]
    assert "User: u2\nAssistant: a2" in fold_prompts[0]
    row = await _session_row(session_factory, session_id)
    messages = await _session_messages(session_factory, session_id)
    assert row.rolling_summary == "S1"
    assert row.summarized_through_id == messages[3].id  # a2


@pytest.mark.db
async def test_empty_summary_output_is_treated_as_a_failed_fold(session_factory):
    # A model returning nothing must not erase the memory it was meant to
    # extend: the watermark stays, so the same turns are folded next time.
    fold_prompts: list[str] = []
    empty = scripted_summary_model(["   "], prompts=fold_prompts)
    with capture_logs() as logs:
        session_id = await _seed_folded_session(session_factory, empty)

    assert len(fold_prompts) == 1
    row = await _session_row(session_factory, session_id)
    assert (row.rolling_summary, row.summarized_through_id) == (None, None)
    failed = [entry for entry in logs if entry["event"] == "rolling_summary_update_failed"]
    assert [entry["error_class"] for entry in failed] == ["EmptySummary"]


@pytest.mark.db
async def test_summarization_changes_only_the_session_row_never_message_rows(session_factory):
    # AC6/R3: persisted ChatMessage rows are byte-identical before and after
    # folding; the summary is derived state on the session row only.
    summarizer = scripted_summary_model(["S1", "S2"])
    session_id = await _seed_folded_session(session_factory, summarizer)

    def snapshot(rows: list[ChatMessage]) -> list[tuple[object, ...]]:
        return [(m.id, m.role, m.content, m.run_id, m.created_at) for m in rows]

    before = snapshot(await _session_messages(session_factory, session_id))
    assert len(before) == 6
    await _collect(
        _summarizing_service(session_factory, summarizer, answer="a4"), "u4", session_id=session_id
    )

    after_rows = await _session_messages(session_factory, session_id)
    after = snapshot(after_rows)
    assert after[:6] == before
    assert [(m.role, m.content) for m in after_rows[6:]] == [
        (MessageRole.USER, "u4"),
        (MessageRole.ASSISTANT, "a4"),
    ]
    assert not any(SUMMARY_PREFIX_LABEL in m.content for m in after_rows)
    assert not any(TRUNCATION_MARKER in m.content for m in after_rows)
    row = await _session_row(session_factory, session_id)
    assert row.rolling_summary == "S2"


@pytest.mark.db
async def test_rolling_summary_logs_carry_numbers_never_text(session_factory):
    # AC7: fold and injection are observable through counts/lengths only —
    # neither the summary nor any turn text reaches the logs.
    summary_text = "FOLDED-ZORBLAT-MEMORY"
    summarizer = scripted_summary_model([summary_text])

    def service(answer: str) -> ChatService:
        return _summarizing_service(session_factory, summarizer, answer=answer, budget=45)

    # Turn costs under `len`: 19, 19, 23, 21 — budget 45 keeps two turns, so
    # turn 3 evicts turn 1; the 21-token summary then reserves 21 of 45.
    events = await _collect(service("quib one"), "zorblat one")
    session_id = events[0].session_id
    assert session_id is not None
    await _collect(service("quib two"), "zorblat two", session_id=session_id)
    with capture_logs() as logs:
        await _collect(service("quib three"), "zorblat three", session_id=session_id)
        await _collect(service("quib four"), "zorblat four", session_id=session_id)

    updated = [entry for entry in logs if entry["event"] == "rolling_summary_updated"]
    assert updated, "expected at least one fold"
    assert updated[0]["folded_turns"] == 1
    assert updated[0]["summary_length"] == len(summary_text)
    assert updated[0]["summary_tokens"] == len(summary_text)
    injected = [entry for entry in logs if entry["event"] == "rolling_summary_injected"]
    assert len(injected) == 1
    assert injected[0]["summary_length"] == len(summary_text)
    assert injected[0]["reserved_tokens"] == len(summary_text)
    rendered = str(logs)
    for secret in ("zorblat one", "quib one", "zorblat four", "quib four", summary_text):
        assert secret not in rendered


def test_rolling_summary_settings_defaults_are_pinned():
    settings = hermetic_settings()
    assert settings.CHAT_ROLLING_SUMMARY_ENABLED is True
    assert settings.CHAT_SUMMARY_MAX_TOKENS == 400


async def test_stateless_service_never_folds_a_summary():
    # Without a session factory there is no history and no session row:
    # a wired summary model is never called (the stateless invariant).
    fold_prompts: list[str] = []
    service = ChatService(
        _make_retriever(),
        scripted_chat_model(answer_parts=["Answer."]),
        mode="hybrid",
        summary_model=scripted_summary_model(["S1"], prompts=fold_prompts),
    )

    events = await _collect(service, QUESTION)

    assert isinstance(events[-1], DoneEvent)
    assert fold_prompts == []


# --- carry prior-run sources into follow-up turns (scripted model; disposable DB) ---


def _two_hit_retriever() -> StubRetriever:
    return StubRetriever(
        outcome=SearchOutcome(
            mode="hybrid",
            items=[
                retrieved_chunk(content="alpha content", document_title="Alpha Doc"),
                retrieved_chunk(chunk_index=1, content="beta content", document_title="Beta Doc"),
            ],
            es_hits=2,
            vector_hits=2,
        )
    )


def _carrying_service(
    factory: async_sessionmaker[AsyncSession],
    retriever: StubRetriever,
    qa_model,
    *,
    carry: bool = True,
) -> ChatService:
    """ChatService wired with carry-sources-forward: scripted QA model, stub
    retriever, one DB session per ask() via the injected factory, `len`
    counter (history assembly stays offline)."""
    return ChatService(
        retriever,
        qa_model,
        mode="hybrid",
        session_factory=factory,
        token_counter=len,
        carry_sources_forward=carry,
    )


async def _assistant_row(
    factory: async_sessionmaker[AsyncSession], session_id: UUID
) -> ChatMessage:
    messages = await _session_messages(factory, session_id)
    assistant = [m for m in messages if m.role is MessageRole.ASSISTANT]
    assert assistant
    return assistant[-1]


def test_collector_hits_flatten_batches_in_order_without_dedup():
    # The persistence view of a run: batches flattened in retrieval order,
    # duplicates kept — replay must preserve the original [1..N] mapping.
    collector = SourceCollector()
    hit = SearchHit(
        document_id=uuid4(),
        document_title="Doc",
        document_tags=[],
        chunk_index=0,
        content="same content",
        score=0.5,
        es_rank=1,
        vector_rank=None,
    )
    collector.append([hit])
    collector.append([hit])

    assert collector.hits == [hit, hit]
    assert collector.total_hits == 2


def test_carried_prefix_renders_numbered_labeled_pair():
    hits = [
        SearchHit.model_validate(
            {
                "document_id": str(uuid4()),
                "document_title": "Alpha Doc",
                "document_tags": [],
                "chunk_index": 0,
                "content": "alpha content",
                "score": 0.5,
                "es_rank": 1,
                "vector_rank": 1,
            }
        )
    ]
    pair = carried_prefix(hits)

    assert isinstance(pair[0], ModelRequest)
    assert isinstance(pair[1], ModelResponse)
    request_text = pair[0].parts[0].content
    assert request_text.startswith(CARRIED_SOURCES_LABEL)
    assert "[1] Alpha Doc" in request_text
    assert "alpha content" in request_text
    assert pair[1].parts[0].content  # the acknowledgement keeps alternation


@pytest.mark.db
async def test_sources_persist_on_assistant_row_in_retrieval_order(session_factory):
    # AC1/AC4: after a retrieval turn the assistant row's `sources` holds the
    # run's hits exactly as retrieved — full SearchHits, order preserved, no
    # dedup; user rows stay NULL.
    retriever = _two_hit_retriever()
    service = _carrying_service(
        session_factory,
        retriever,
        scripted_chat_model(tool_calls=["alpha"], answer_parts=["Answer [1]."]),
    )
    events = await _collect(service, QUESTION)
    session_id = events[0].session_id
    assert session_id is not None

    messages = await _session_messages(session_factory, session_id)
    assert messages[0].sources is None  # the user row never carries sources
    row = await _assistant_row(session_factory, session_id)
    assert row.sources is not None
    hits = [SearchHit.model_validate(item) for item in row.sources]
    assert [(hit.document_title, hit.content, hit.chunk_index) for hit in hits] == [
        ("Alpha Doc", "alpha content", 0),
        ("Beta Doc", "beta content", 1),
    ]


@pytest.mark.db
async def test_followup_reemits_carried_sources_as_first_batch_and_leading_pair(
    session_factory,
):
    # AC2/AC3/AC4: the follow-up turn emits the previous run's sources as the
    # FIRST `sources` batch (right after run_started, before fresh batches),
    # seeds the collector so fresh numbering continues after them, and shows
    # the model a labeled carried pair numbered [1..k] ahead of fresh blocks.
    turn1_model = scripted_chat_model(
        tool_calls=["alpha"],
        answer_parts=["Answer [1]."],
        tool_results=[],
    )
    events1 = await _collect(
        _carrying_service(session_factory, _two_hit_retriever(), turn1_model), QUESTION
    )
    session_id = events1[0].session_id
    assert session_id is not None

    tool_results: list[str] = []
    qa_histories: list[list[ModelMessage]] = []
    retriever = _two_hit_retriever()
    turn2_model = scripted_chat_model(
        tool_calls=["follow-up"],
        answer_parts=["Follow-up answer [3]."],
        tool_results=tool_results,
        histories=qa_histories,
    )
    with capture_logs() as logs:
        events2 = await _collect(
            _carrying_service(session_factory, retriever, turn2_model),
            "tell me more about [1]",
            session_id=session_id,
        )

    # First batch immediately after run_started; fresh batch after it. No
    # progress event may appear between run_started and the carried batch (AC5).
    assert _names(events2) == [
        "RunStartedEvent",
        "SourcesEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    first, fresh = (event for event in events2 if isinstance(event, SourcesEvent))
    assert [hit.document_title for hit in first.items] == ["Alpha Doc", "Beta Doc"]
    assert [hit.content for hit in first.items] == ["alpha content", "beta content"]
    # AC4: carried content/order is exactly the previous run's citations
    # (turn 1's row — turn 2's row carries carried + fresh, 4 hits).
    messages = await _session_messages(session_factory, session_id)
    turn1_sources = next(
        m.sources for m in messages if m.role is MessageRole.ASSISTANT and m.sources
    )
    assert first.items == [SearchHit.model_validate(item) for item in turn1_sources]
    row = await _assistant_row(session_factory, session_id)
    assert row.sources is not None and len(row.sources) == 4
    # Fresh retrieval returned the same two hits, numbered [3] and [4].
    assert [hit.document_title for hit in fresh.items] == ["Alpha Doc", "Beta Doc"]
    assert tool_results[0].startswith("[3] Alpha Doc")

    # AC3: the model's prompt opens with the labeled carried pair ([1..2]),
    # then the current prompt; the fresh tool result continued at [3].
    assert qa_histories
    for messages in qa_histories:
        prompts = _user_prompts(messages)
        assert prompts[0].startswith(CARRIED_SOURCES_LABEL)
        assert "[1] Alpha Doc" in prompts[0]
        assert "[2] Beta Doc" in prompts[0]
        assert isinstance(messages[0], ModelRequest)
        assert isinstance(messages[1], ModelResponse)
        assert messages[1].parts[0].content  # acknowledgement keeps alternation
        # The run prompt follows the carried pair (history precedes it).
        assert prompts[-1] == "tell me more about [1]"

    # AC7: counts only — no source or question text in the logs; the run
    # reports the carried count.
    assert "alpha content" not in str(logs)
    assert "tell me more about [1]" not in str(logs)
    finished = next(entry for entry in logs if entry["event"] == "agent_run_finished")
    assert finished["carried_sources"] == 2


@pytest.mark.db
async def test_carry_disabled_keeps_old_behavior_byte_identical(session_factory):
    # AC5: without the flag (the constructor default and the Settings-off
    # wiring) nothing is written, emitted, or prepended — the two turns are
    # exactly the pre-carry service, including the log surface.
    turn1_model = scripted_chat_model(
        tool_calls=["alpha"], answer_parts=["Answer [1]."], tool_results=[]
    )
    events1 = await _collect(
        _carrying_service(session_factory, _two_hit_retriever(), turn1_model, carry=False),
        QUESTION,
    )
    session_id = events1[0].session_id
    assert session_id is not None

    tool_results: list[str] = []
    qa_histories: list[list[ModelMessage]] = []
    turn2_model = scripted_chat_model(
        tool_calls=["follow-up"],
        answer_parts=["Follow-up answer."],
        tool_results=tool_results,
        histories=qa_histories,
    )
    with capture_logs() as logs:
        events2 = await _collect(
            _carrying_service(session_factory, _two_hit_retriever(), turn2_model, carry=False),
            "tell me more",
            session_id=session_id,
        )

    # One fresh sources batch only — no carried batch ahead of it.
    assert _names(events2) == [
        "RunStartedEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert tool_results[0].startswith("[1] Alpha Doc")  # numbering restarts at 1
    assert qa_histories
    for messages in qa_histories:
        assert all(
            not (isinstance(part, UserPromptPart) and isinstance(part.content, str))
            or not part.content.startswith(CARRIED_SOURCES_LABEL)
            for message in messages
            for part in message.parts
        )
    # Nothing was persisted and the finished log carries no carried count.
    row = await _assistant_row(session_factory, session_id)
    assert row.sources is None
    finished = next(entry for entry in logs if entry["event"] == "agent_run_finished")
    assert "carried_sources" not in finished


@pytest.mark.db
async def test_null_sources_prior_turn_carries_nothing(session_factory):
    # AC6: a prior turn from before the feature (or with empty sources) is
    # simply not carried — the next carry-enabled turn behaves like a first.
    turn1_model = scripted_chat_model(
        tool_calls=["alpha"], answer_parts=["Answer [1]."], tool_results=[]
    )
    events1 = await _collect(
        _carrying_service(session_factory, _two_hit_retriever(), turn1_model, carry=False),
        QUESTION,
    )
    session_id = events1[0].session_id
    assert session_id is not None

    tool_results: list[str] = []
    qa_histories: list[list[ModelMessage]] = []
    turn2_model = scripted_chat_model(
        tool_calls=["follow-up"],
        answer_parts=["Follow-up answer."],
        tool_results=tool_results,
        histories=qa_histories,
    )
    events2 = await _collect(
        _carrying_service(session_factory, _two_hit_retriever(), turn2_model),
        "tell me more",
        session_id=session_id,
    )

    # Flag is ON here, but there was nothing to carry: one fresh batch only.
    assert _names(events2) == [
        "RunStartedEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert tool_results[0].startswith("[1] Alpha Doc")
    assert qa_histories
    for messages in qa_histories:
        assert not _user_prompts(messages)[0].startswith(CARRIED_SOURCES_LABEL)


async def test_stateless_service_never_carries_or_prepends():
    # AC6: without a session factory nothing is ever carried (and nothing is
    # persisted) — the stateless stream is unchanged by the feature.
    qa_histories: list[list[ModelMessage]] = []
    service = ChatService(
        _two_hit_retriever(),
        scripted_chat_model(
            tool_calls=["zorblat"],
            answer_parts=ANSWER_PARTS,
            histories=qa_histories,
        ),
        mode="hybrid",
        carry_sources_forward=True,
    )

    events = await _collect(service, QUESTION)

    assert _names(events) == [
        "RunStartedEvent",
        "ToolCallStartedEvent",
        "SourcesEvent",
        "ToolCallFinishedEvent",
        "StatusEvent",
        "AnswerDeltaEvent",
        "AnswerDeltaEvent",
        "DoneEvent",
    ]
    assert qa_histories
    for messages in qa_histories:
        assert not _user_prompts(messages)[0].startswith(CARRIED_SOURCES_LABEL)


def test_sources_carry_settings_defaults_are_pinned():
    settings = hermetic_settings()
    assert settings.CHAT_SOURCES_CARRY_ENABLED is True
