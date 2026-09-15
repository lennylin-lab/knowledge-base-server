"""Summarize service semantics (offline model, real test DB).

The model is always a scripted FunctionModel; provider failures use the
openai SDK's own exception types so the service's error mapping is exercised
for real. Chunking is the production chunker — the map-reduce tests rely on
its deterministic split.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import openai
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.core.exceptions import LLMProviderError, LLMRateLimitedError, NotFoundError
from app.models.tenant import DEFAULT_TENANT_ID
from app.schemas.agent_stream import (
    AgentDoneEvent,
    AgentRunStartedEvent,
    ErrorEvent,
    SummaryProgressEvent,
    SummaryResultEvent,
)
from app.schemas.document import DocumentCreate
from app.services.agents import SummarizeService
from app.services.document import DocumentService
from fakes import FakeCache, scripted_summarize_model

pytestmark = pytest.mark.db

MODEL_NAME = "test-chat-model"
SHORT_DOC = "---\ntitle: Short Note\ntags: [alpha]\n---\n\nOne short paragraph about zorblat."


def long_content(sections: int = 3) -> str:
    """Sections each exceed the chunker's ~800-char target (and pairwise sums
    exceed its 1600 cap), so every section lands in its own chunk."""
    body = "The quibnard decision changed everything for the zorblat team. " * 18
    return "\n\n".join(f"# Section {i}\n\n{body}" for i in range(1, sections + 1))


def make_service(
    session_factory: async_sessionmaker[AsyncSession], model: FunctionModel
) -> SummarizeService:
    return SummarizeService(model, MODEL_NAME, session_factory=session_factory)


async def make_document(session: AsyncSession, content: str) -> object:
    return await DocumentService(session).create_document(
        DocumentCreate(content=content), tenant_id=DEFAULT_TENANT_ID
    )


async def test_short_document_summarizes_in_one_pass(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_summarize_model(["Scripted summary."], prompts=prompts)
    )

    result = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    assert result.document_id == created.id
    assert result.summary == "Scripted summary."
    assert result.model == MODEL_NAME
    assert result.latency_ms >= 0
    # Short doc: a single pass, and it carried the document context.
    assert len(prompts) == 1
    assert "# Document: Short Note" in prompts[0]
    assert "alpha" in prompts[0]  # tags ride along as prompt context
    assert "zorblat" in prompts[0]
    assert "Section" not in prompts[0]  # no map-pass marker on the direct path


async def test_long_document_map_reduces_over_chunk_summaries(db_session, session_factory):
    created = await make_document(
        db_session, f"---\ntitle: Long Note\ntags: [long]\n---\n\n{long_content(3)}"
    )
    prompts: list[str] = []
    service = make_service(
        session_factory,
        scripted_summarize_model(["s-one", "s-two", "s-three", "Final reduce."], prompts=prompts),
    )

    result = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    assert result.summary == "Final reduce."  # the combine pass wins, not a chunk
    assert len(prompts) == 4  # 3 sequential chunk passes + 1 combine (no fan-out)
    # Each chunk pass saw exactly one section, in document order, marked as such.
    assert "# Section 1" in prompts[0] and "Section 1 of 3" in prompts[0]
    assert "# Section 2" not in prompts[0]
    assert "# Section 2" in prompts[1]
    assert "# Section 3" in prompts[2]
    # The combine pass received every chunk summary, not the raw content.
    assert "# Document: Long Note" in prompts[3]
    assert "quibnard" not in prompts[3]  # raw chunk text never reaches the combine pass
    assert "s-one" in prompts[3]
    assert "s-two" in prompts[3]
    assert "s-three" in prompts[3]
    assert result.latency_ms >= 0


async def test_missing_document_raises_not_found_before_any_model_call(session_factory):
    prompts: list[str] = []
    service = make_service(session_factory, scripted_summarize_model(["never"], prompts=prompts))

    with pytest.raises(NotFoundError):
        await service.summarize_document(uuid4(), tenant_id=DEFAULT_TENANT_ID)

    assert prompts == []  # the 404 gate fires before any LLM call


async def test_soft_deleted_document_raises_not_found(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    await DocumentService(db_session).delete_document(created.id, tenant_id=DEFAULT_TENANT_ID)
    prompts: list[str] = []
    service = make_service(session_factory, scripted_summarize_model(["never"], prompts=prompts))

    with pytest.raises(NotFoundError):
        await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    assert prompts == []


async def test_run_lifecycle_is_logged_with_document_id_and_no_content(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    service = make_service(session_factory, scripted_summarize_model(["Scripted summary."]))

    with capture_logs() as logs:
        await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    started = next(entry for entry in logs if entry["event"] == "agent_run_started")
    finished = next(entry for entry in logs if entry["event"] == "agent_run_finished")
    assert started["agent"] == "summarize"
    assert started["content_length"] == len(SHORT_DOC)
    assert started["run_id"]
    assert finished["agent"] == "summarize"
    assert finished["outcome"] == "success"
    assert finished["runs"] == 1
    assert finished["model"] == MODEL_NAME
    assert all(entry.get("document_id") == str(created.id) for entry in logs)
    # Content and summary text are user data: never logged at any level.
    assert "zorblat" not in str(logs)
    assert "Scripted summary." not in str(logs)


async def test_provider_failure_wraps_into_llm_provider_error_and_logs_failure(
    db_session, session_factory
):
    created = await make_document(db_session, SHORT_DOC)

    async def failing(messages: list[ModelMessage], info: object) -> ModelResponse:
        raise openai.APIStatusError(
            "upstream exploded with secret detail",
            response=httpx.Response(
                500, request=httpx.Request("POST", "http://provider.test/v1/chat")
            ),
            body=None,
        )

    service = SummarizeService(
        FunctionModel(failing, model_name="failing"),
        MODEL_NAME,
        session_factory=session_factory,
    )

    with capture_logs() as logs, pytest.raises(LLMProviderError) as exc_info:
        await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    # Provider internals stay out of the response; they went to logs only.
    assert "upstream exploded" not in exc_info.value.message
    failed = next(entry for entry in logs if entry["event"] == "agent_run_failed")
    assert failed["agent"] == "summarize"
    assert failed["outcome"] == "llm_provider_error"
    assert failed["error_class"] == "APIStatusError"
    assert not any(entry["event"] == "agent_run_finished" for entry in logs)


async def test_rate_limit_failure_maps_to_llm_rate_limited(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)

    async def failing(messages: list[ModelMessage], info: object) -> ModelResponse:
        raise openai.RateLimitError(
            "rate limited after secret detail",
            response=httpx.Response(
                429, request=httpx.Request("POST", "http://provider.test/v1/chat")
            ),
            body=None,
        )

    service = SummarizeService(
        FunctionModel(failing, model_name="failing"),
        MODEL_NAME,
        session_factory=session_factory,
    )

    with capture_logs() as logs, pytest.raises(LLMRateLimitedError) as exc_info:
        await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    # Rate limit must win over the generic APIError branch, message stays generic.
    assert "secret detail" not in exc_info.value.message
    assert exc_info.value.message == "LLM provider rate limit exceeded"
    failed = next(entry for entry in logs if entry["event"] == "agent_run_failed")
    assert failed["outcome"] == "rate_limited"
    assert failed["error_class"] == "RateLimitError"


async def test_model_http_error_maps_to_llm_provider_error(db_session, session_factory):
    """What the production model raises: pydantic-ai wraps provider HTTP
    failures in ModelHTTPError, which must reach the taxonomy (not the
    generic 500) — regression for the FunctionModel-only blind spot."""
    created = await make_document(db_session, SHORT_DOC)

    async def failing(messages: list[ModelMessage], info: object) -> ModelResponse:
        raise ModelHTTPError(
            status_code=503,
            model_name="failing",
            body={"message": "upstream exploded"},
        )

    service = SummarizeService(
        FunctionModel(failing, model_name="failing"),
        MODEL_NAME,
        session_factory=session_factory,
    )

    with capture_logs() as logs, pytest.raises(LLMProviderError) as exc_info:
        await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    # Provider internals stay out of the response; they went to logs only.
    assert "upstream exploded" not in exc_info.value.message
    failed = next(entry for entry in logs if entry["event"] == "agent_run_failed")
    assert failed["outcome"] == "llm_provider_error"
    assert failed["error_class"] == "ModelHTTPError"
    assert not any(entry["event"] == "agent_run_finished" for entry in logs)


async def test_model_http_429_maps_to_llm_rate_limited(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)

    async def failing(messages: list[ModelMessage], info: object) -> ModelResponse:
        raise ModelHTTPError(status_code=429, model_name="failing", body=None)

    service = SummarizeService(
        FunctionModel(failing, model_name="failing"),
        MODEL_NAME,
        session_factory=session_factory,
    )

    with capture_logs() as logs, pytest.raises(LLMRateLimitedError) as exc_info:
        await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    # 429 must win over the generic provider-error branch, message stays generic.
    assert exc_info.value.message == "LLM provider rate limit exceeded"
    failed = next(entry for entry in logs if entry["event"] == "agent_run_failed")
    assert failed["outcome"] == "rate_limited"
    assert failed["error_class"] == "ModelHTTPError"


async def test_front_matter_only_document_summarizes_raw_content_in_one_pass(
    db_session, session_factory
):
    created = await make_document(db_session, "---\ntitle: Only Front Matter\ntags: [meta]\n---\n")
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_summarize_model(["Degenerate summary."], prompts=prompts)
    )

    result = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    assert result.summary == "Degenerate summary."
    # Degenerate path: chunk_markdown strips to nothing, so the single pass goes
    # over the raw stored content (YAML front matter included) — never empty.
    assert len(prompts) == 1
    assert "# Document: Only Front Matter" in prompts[0]
    assert "title: Only Front Matter" in prompts[0]


# --- stream contract (design: agent stream) ---


async def drain(events):
    """Collect a typed event stream into a list."""
    return [event async for event in events]


async def test_stream_success_single_pass_emits_fixed_grammar(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    service = make_service(session_factory, scripted_summarize_model(["Streamed summary."]))

    events = await drain(service.summarize_document_stream(created.id, tenant_id=DEFAULT_TENANT_ID))

    # Fixed grammar even for a single pass: one map + one reduce progress.
    assert [type(event) for event in events] == [
        AgentRunStartedEvent,
        SummaryProgressEvent,
        SummaryProgressEvent,
        SummaryResultEvent,
        AgentDoneEvent,
    ]
    run_started = events[0]
    assert run_started.kind == "summary"
    assert run_started.document_id == created.id
    assert run_started.run_id
    assert events[1].model_dump() == {"phase": "map_pass", "pass_index": 1, "passes_total": 2}
    assert events[2].model_dump() == {"phase": "reduce_pass", "pass_index": 2, "passes_total": 2}
    result = events[3]
    assert result.summary == "Streamed summary."
    assert result.document_id == created.id
    assert result.model == MODEL_NAME
    assert result.latency_ms >= 0
    done = events[4]
    assert done.run_id == run_started.run_id
    assert done.outcome == "success"
    assert done.latency_ms == result.latency_ms


async def test_stream_multipass_progress_indices_follow_pass_order(db_session, session_factory):
    created = await make_document(
        db_session, f"---\ntitle: Long Note\ntags: [long]\n---\n\n{long_content(3)}"
    )
    service = make_service(
        session_factory, scripted_summarize_model(["s-one", "s-two", "s-three", "Final reduce."])
    )

    events = await drain(service.summarize_document_stream(created.id, tenant_id=DEFAULT_TENANT_ID))

    progress = [event for event in events if isinstance(event, SummaryProgressEvent)]
    assert [(event.phase, event.pass_index, event.passes_total) for event in progress] == [
        ("map_pass", 1, 4),
        ("map_pass", 2, 4),
        ("map_pass", 3, 4),
        ("reduce_pass", 4, 4),
    ]
    # The final result still carries the reduce pass output.
    assert events[-2].summary == "Final reduce."


async def test_stream_cache_hit_skips_progress_events_and_model_call(db_session, session_factory):
    prompts: list[str] = []
    model = scripted_summarize_model(["Cached summary."], prompts=prompts)
    cache = FakeCache()
    created = await make_document(db_session, SHORT_DOC)
    warming = SummarizeService(
        model, MODEL_NAME, session_factory=session_factory, cache=cache, cache_ttl_seconds=60
    )
    await warming.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    events = await drain(warming.summarize_document_stream(created.id, tenant_id=DEFAULT_TENANT_ID))

    assert [type(event) for event in events] == [
        AgentRunStartedEvent,
        SummaryResultEvent,
        AgentDoneEvent,
    ]
    assert events[1].summary == "Cached summary."
    assert len(prompts) == 1  # the warm run only; the hit made no model call


async def test_stream_provider_failure_emits_single_terminal_error(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)

    async def failing(messages: list[ModelMessage], info: object) -> ModelResponse:
        raise ModelHTTPError(
            status_code=503, model_name="failing", body={"message": "upstream exploded"}
        )

    service = SummarizeService(
        FunctionModel(failing, model_name="failing"),
        MODEL_NAME,
        session_factory=session_factory,
    )

    with capture_logs() as logs:
        events = await drain(
            service.summarize_document_stream(created.id, tenant_id=DEFAULT_TENANT_ID)
        )

    # run_started stands, then one progress event, then exactly one terminal
    # error event and no done.
    assert [type(event) for event in events] == [
        AgentRunStartedEvent,
        SummaryProgressEvent,
        ErrorEvent,
    ]
    assert events[-1].code == "llm_provider_error"
    assert "upstream exploded" not in events[-1].message
    failed = next(entry for entry in logs if entry["event"] == "agent_run_failed")
    assert failed["outcome"] == "llm_provider_error"


async def test_stream_missing_document_raises_before_first_event(session_factory):
    service = make_service(session_factory, scripted_summarize_model(["never"]))

    with pytest.raises(NotFoundError):
        await drain(service.summarize_document_stream(uuid4(), tenant_id=DEFAULT_TENANT_ID))


async def test_wrapper_result_matches_stream_result_event(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    service = make_service(session_factory, scripted_summarize_model(["Parity summary."]))

    wrapper_result = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)
    stream_events = await drain(
        service.summarize_document_stream(created.id, tenant_id=DEFAULT_TENANT_ID)
    )
    stream_result = next(e for e in stream_events if isinstance(e, SummaryResultEvent))

    assert wrapper_result.summary == stream_result.summary
    assert wrapper_result.model_dump(exclude={"latency_ms"}) == stream_result.model_dump(
        exclude={"latency_ms"}
    )
