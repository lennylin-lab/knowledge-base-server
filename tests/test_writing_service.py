"""Writing service stream semantics (offline): optional retrieval, failures.

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

from app.agents.writing import DEFAULT_INSTRUCTION, render_writing_prompt
from app.core.exceptions import SearchIndexError
from app.rag.retriever import RetrievedChunk, SearchOutcome
from app.schemas.chat import (
    AnswerDeltaEvent,
    DoneEvent,
    ErrorEvent,
    RunStartedEvent,
    SourcesEvent,
)
from app.services.agents import WritingService
from fakes import StubRetriever, retrieved_chunk, scripted_chat_model

DRAFT = "# Kotlin coroutines\n\nNotes on how zorblat suspension works."
INSTRUCTION = "rewrite in a terser voice"
ANSWER_PARTS = ["The zorblat suspension point ", "yields to the caller [1]."]


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
    service: WritingService, draft: str, instruction: str | None = None, *, limit: int = 8
) -> list[object]:
    return [event async for event in service.suggest(draft, instruction, limit=limit)]


def _names(events: list[object]) -> list[str]:
    return [type(event).__name__ for event in events]


def _answer_text(events: list[object]) -> str:
    return "".join(event.text for event in events if isinstance(event, AnswerDeltaEvent))


def test_render_writing_prompt_uses_the_default_instruction_when_absent():
    prompt = render_writing_prompt(DRAFT)

    assert DRAFT in prompt  # the draft rides verbatim
    assert DEFAULT_INSTRUCTION in prompt
    assert "# Draft" in prompt and "# Instruction" in prompt


def test_render_writing_prompt_carries_explicit_instruction_verbatim():
    prompt = render_writing_prompt(DRAFT, INSTRUCTION)

    assert DRAFT in prompt
    assert prompt.count(INSTRUCTION) == 1
    assert DEFAULT_INSTRUCTION not in prompt


def test_render_writing_prompt_treats_empty_instruction_as_absent():
    # Documented deviation: an empty instruction (not just a missing one)
    # falls back to the default — one instruction contract, no empty edge.
    assert render_writing_prompt(DRAFT, "") == render_writing_prompt(DRAFT)


async def test_suggest_with_tool_call_streams_started_sources_deltas_done_in_order():
    retriever = _make_retriever(
        retrieved_chunk(content="zorblat everywhere", document_title="Kotlin Notes")
    )
    prompts: list[str] = []
    service = WritingService(
        retriever,
        scripted_chat_model(
            tool_calls=["zorblat"],
            answer_parts=ANSWER_PARTS,
            prompts=prompts,
        ),
        mode="hybrid",
    )

    events = await _collect(service, DRAFT, limit=5)

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
    assert retriever.calls == [("zorblat", 5)]
    # The model saw the draft verbatim plus the default instruction.
    assert prompts and DRAFT in prompts[0]
    assert DEFAULT_INSTRUCTION in prompts[0]

    assert _answer_text(events) == "The zorblat suspension point yields to the caller [1]."
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.outcome == "success"
    assert done.tool_calls == 1
    assert done.run_id == run_started.run_id
    assert done.latency_ms >= 0


async def test_suggest_instruction_reaches_the_model():
    prompts: list[str] = []
    service = WritingService(
        StubRetriever(),
        scripted_chat_model(answer_parts=["Terser."], prompts=prompts),
        mode="hybrid",
    )

    events = await _collect(service, DRAFT, INSTRUCTION)

    assert _names(events) == ["RunStartedEvent", "AnswerDeltaEvent", "DoneEvent"]
    assert prompts[0].count(INSTRUCTION) == 1
    assert DEFAULT_INSTRUCTION not in prompts[0]


async def test_suggest_without_tool_calls_has_no_sources_event():
    service = WritingService(
        StubRetriever(),
        scripted_chat_model(answer_parts=["Continued from general competence."]),
        mode="bm25",
    )

    events = await _collect(service, DRAFT)

    # Retrieval is optional: a zero-tool run streams straight to text.
    assert _names(events) == ["RunStartedEvent", "AnswerDeltaEvent", "DoneEvent"]
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].mode == "bm25"
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.tool_calls == 0


async def test_empty_retrieval_still_emits_an_empty_sources_event():
    service = WritingService(
        StubRetriever(),
        scripted_chat_model(tool_calls=["nothing-matches"], answer_parts=["Nothing found."]),
        mode="hybrid",
    )

    events = await _collect(service, DRAFT)

    sources = [event for event in events if isinstance(event, SourcesEvent)]
    assert len(sources) == 1
    assert sources[0].items == []


async def test_each_retrieval_flushes_its_own_sources_event():
    retriever = _make_retriever()
    service = WritingService(
        retriever,
        scripted_chat_model(tool_calls=["zorblat", "quibnard"], answer_parts=ANSWER_PARTS),
        mode="hybrid",
    )

    events = await _collect(service, DRAFT)

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


async def test_citation_numbers_continue_across_retrievals():
    # The numbering logic is writing's own WritingDeps twin of QA's tool (kept
    # local by design), so it needs its own pin: clients concatenate `sources`
    # events, and a second retrieval restarting at [1] would make the bracketed
    # citations in the suggestion ambiguous.
    tool_results: list[str] = []
    service = WritingService(
        _make_retriever(),
        scripted_chat_model(
            tool_calls=["zorblat", "quibnard"],
            answer_parts=ANSWER_PARTS,
            tool_results=tool_results,
        ),
        mode="hybrid",
    )

    events = await _collect(service, DRAFT)

    assert len(tool_results) == 2
    assert tool_results[0].startswith("[1] ")
    assert tool_results[1].startswith("[2] ")  # continues, does not restart at [1]
    sources = [event for event in events if isinstance(event, SourcesEvent)]
    assert [len(event.items) for event in sources] == [1, 1]
    # Concatenated source order matches the citation numbers the model saw.
    assert tool_results[0].startswith("[1] Notes")
    assert tool_results[1].startswith("[2] Notes")


async def test_provider_failure_mid_stream_ends_with_terminal_error_event():
    provider_error = openai.APIStatusError(
        "upstream exploded with secret detail",
        response=httpx.Response(500, request=httpx.Request("POST", "http://provider.test/v1/chat")),
        body=None,
    )
    service = WritingService(
        StubRetriever(),
        scripted_chat_model(
            tool_calls=["zorblat"],
            answer_parts=["partial suggestion ", "never streamed"],
            fail_during_answer=provider_error,
            fail_after_parts=1,
        ),
        mode="hybrid",
    )

    events = await _collect(service, DRAFT)

    assert _names(events) == ["RunStartedEvent", "SourcesEvent", "AnswerDeltaEvent", "ErrorEvent"]
    assert _answer_text(events) == "partial suggestion "
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "llm_provider_error"
    # Provider internals stay out of the stream; they went to logs only.
    assert "upstream exploded" not in error.message
    assert not any(isinstance(event, DoneEvent) for event in events)


async def test_search_index_error_from_the_tool_is_never_answered_around():
    service = WritingService(
        StubRetriever(
            error=SearchIndexError("index unavailable", details={"operation": "search_chunks"})
        ),
        scripted_chat_model(tool_calls=["zorblat"], answer_parts=["ungrounded suggestion"]),
        mode="hybrid",
    )

    events = await _collect(service, DRAFT)

    assert _names(events) == ["RunStartedEvent", "ErrorEvent"]
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "search_index_error"
    assert _answer_text(events) == ""  # never an ungrounded suggestion


async def test_unknown_failure_maps_to_generic_internal_error():
    service = WritingService(
        StubRetriever(),
        scripted_chat_model(
            answer_parts=["partial"],
            fail_during_answer=RuntimeError("secret internal detail"),
            fail_after_parts=0,
        ),
        mode="hybrid",
    )

    events = await _collect(service, DRAFT)

    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "internal_error"
    assert error.message == "Internal server error"
    assert "secret internal detail" not in error.message


async def test_run_id_is_bound_into_events_and_logs_and_draft_never_logged():
    service = WritingService(
        _make_retriever(),
        scripted_chat_model(tool_calls=["zorblat"], answer_parts=ANSWER_PARTS),
        mode="hybrid",
    )

    # capture_logs replaces the processor chain, so merge_contextvars (which
    # carries the run_id binding into events) must be passed explicitly.
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        events = await _collect(service, DRAFT, INSTRUCTION)

    run_id = events[0].run_id if isinstance(events[0], RunStartedEvent) else None
    assert run_id
    assert logs, "expected agent lifecycle logs"
    assert all(entry.get("run_id") == run_id for entry in logs)
    assert any(entry["event"] == "agent_run_started" for entry in logs)
    assert any(entry["event"] == "agent_run_finished" for entry in logs)
    started = next(entry for entry in logs if entry["event"] == "agent_run_started")
    assert started["agent"] == "writing"
    assert started["draft_length"] == len(DRAFT)
    assert started["instruction_length"] == len(INSTRUCTION)
    # Draft and instruction are user data: never logged at any level.
    assert "how zorblat suspension works" not in str(logs)
    assert INSTRUCTION not in str(logs)
