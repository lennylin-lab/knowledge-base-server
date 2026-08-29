"""Chat service stream semantics (offline): event order, sources, failures.

The model is always a scripted FunctionModel and the retriever a stub — no
live LLM, no infrastructure. Provider failures are scripted with the openai
SDK's own exception types so the service's error mapping is exercised for
real.
"""

from __future__ import annotations

import httpx
import openai
import structlog
from structlog.testing import capture_logs

from app.agents.qa import SourceCollector
from app.core.exceptions import SearchIndexError
from app.mcp.manager import McpToolInfo, McpToolResult
from app.mcp.tools import build_agent_tools
from app.rag.retriever import RetrievedChunk, SearchOutcome
from app.schemas.chat import (
    AnswerDeltaEvent,
    DoneEvent,
    ErrorEvent,
    RunStartedEvent,
    SourcesEvent,
)
from app.services.chat import ChatService
from fakes import (
    FakeMcpManager,
    StubRetriever,
    retrieved_chunk,
    scripted_chat_model,
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


async def _collect(service: ChatService, question: str, *, limit: int = 8) -> list[object]:
    return [event async for event in service.ask(question, limit=limit)]


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
