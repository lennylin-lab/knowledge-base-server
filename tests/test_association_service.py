"""Association service semantics (offline model, real test DB).

Deterministic candidate gathering is exercised against the corpus seeded
through the real IndexingPipeline (seed_indexed + scripted embeddings), so
each leg's behavior — neighbor found, tag match found, self excluded,
soft-deleted invisible — is verified against known vectors. The LLM leg is
always a scripted FunctionModel returning the agent's structured output tool
call, so hallucinated-id dropping runs through the real join.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import openai
import pytest
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.core.exceptions import AppError, LLMProviderError, NotFoundError
from app.models.tenant import DEFAULT_TENANT_ID
from app.repositories.document_chunk import DocumentChunkRepository
from app.schemas.agent_stream import (
    AgentDoneEvent,
    AgentRunStartedEvent,
    AssociationsResultEvent,
    ErrorEvent,
)
from app.schemas.document import DocumentCreate
from app.services.agents import AssociationService
from app.services.document import DocumentService
from corpus import KOTLIN_CONTENT, PYTHON_CONTENT, neighbor_scripted_provider, seed_corpus
from fakes import FakeCache, basis_vector, scripted_association_model

pytestmark = pytest.mark.db

MODEL_NAME = "test-chat-model"

# Tag-overlap corpus contents (front-matter titles/tags are what matters).
ALPHA_SOURCE = (
    "---\ntitle: Alpha Notes\ntags: [alpha]\n---\n\n"
    "One short paragraph about the zorblat alpha process."
)
ALPHA_NEIGHBOR = (
    "---\ntitle: Alpha Companion\ntags: [alpha, other]\n---\n\n"
    "One short paragraph about the quibnard companion notes."
)
UNRELATED = (
    "---\ntitle: Zeta Notes\ntags: [zeta]\n---\n\n"
    "One short paragraph about unrelated zeta material."
)

# JVM corpus addition: shares the Kotlin doc's tag, indexed with the
# provider default embedding (orthogonal to Kotlin's scripted vector).
JVM_CONTENT = (
    "---\ntitle: JVM Internals\ntags: [kotlin, jvm]\n---\n\n"
    f"# JVM notes\n\n{('quibnard ' * 130).strip()}"
)


def make_service(
    session_factory: async_sessionmaker[AsyncSession], model: FunctionModel
) -> AssociationService:
    return AssociationService(model, MODEL_NAME, session_factory=session_factory)


def pick(document_id: UUID, reason: str = "Scripted reason.") -> dict[str, str]:
    """One structured-output pick dict as the scripted model returns it."""
    return {"document_id": str(document_id), "reason": reason}


async def make_document(session: AsyncSession, content: str) -> object:
    return await DocumentService(session).create_document(
        DocumentCreate(content=content), tenant_id=DEFAULT_TENANT_ID
    )


async def soft_delete(session_factory: async_sessionmaker[AsyncSession], doc_id: UUID) -> None:
    """Soft-delete via the service on a fresh session (commits the delete)."""
    async with session_factory() as session:
        await DocumentService(session).delete_document(doc_id, tenant_id=DEFAULT_TENANT_ID)


async def seed_three_docs(seed_indexed, second: str, third: str) -> tuple[UUID, UUID, UUID]:
    """Seed the Kotlin source plus two more docs through one provider."""
    provider = neighbor_scripted_provider()
    kotlin_id = await seed_indexed(provider, KOTLIN_CONTENT)
    second_id = await seed_indexed(provider, second)
    third_id = await seed_indexed(provider, third)
    return kotlin_id, second_id, third_id


@pytest.mark.es
async def test_vector_leg_surfaces_neighbor_and_excludes_self(session_factory, seed_indexed):
    kotlin_id, python_id = await seed_corpus(seed_indexed, neighbor_scripted_provider())
    prompts: list[str] = []
    service = make_service(
        session_factory,
        scripted_association_model([pick(python_id, "Same-notebook neighbor.")], prompts=prompts),
    )

    result = await service.associate_document(kotlin_id, tenant_id=DEFAULT_TENANT_ID)

    # The vector leg surfaced exactly the Python doc; the join carried its
    # deterministic metadata plus the scripted reason.
    assert [item.document_id for item in result.associations] == [python_id]
    item = result.associations[0]
    assert item.title == "Python Notes"
    assert item.tags == ["python"]
    assert item.reason == "Same-notebook neighbor."
    assert "cosine distance" in item.signal
    assert "shared tags" not in item.signal  # disjoint tags: vector signal only
    assert result.model == MODEL_NAME
    assert result.latency_ms >= 0
    # Prompt grounding: the neighbor is a candidate (with its id), the source
    # document never is, and no tag signal was invented.
    assert len(prompts) == 1
    assert f"id={python_id}" in prompts[0]
    assert f"id={kotlin_id}" not in prompts[0]
    assert "shared tags" not in prompts[0]


@pytest.mark.es
async def test_tag_leg_surfaces_shared_tag_document(session_factory, seed_indexed):
    kotlin_id, jvm_id, _ = await seed_three_docs(seed_indexed, JVM_CONTENT, UNRELATED)
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(jvm_id)], prompts=prompts)
    )

    result = await service.associate_document(kotlin_id, tenant_id=DEFAULT_TENANT_ID)

    assert [item.document_id for item in result.associations] == [jvm_id]
    assert result.associations[0].title == "JVM Internals"
    # The Kotlin doc shares exactly the `kotlin` tag with the JVM doc.
    assert "shared tags: kotlin" in result.associations[0].signal
    assert f"id={jvm_id}" in prompts[0]


async def test_unindexed_source_degrades_to_tag_only_candidates(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    await make_document(db_session, UNRELATED)
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(neighbor.id)], prompts=prompts)
    )

    result = await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)

    # No chunks on the source: the vector leg cannot run, the tag leg still
    # delivers candidates — no crash, no empty result while tags match.
    assert [item.document_id for item in result.associations] == [neighbor.id]
    assert result.associations[0].signal == "shared tags: alpha"
    assert len(prompts) == 1
    assert "cosine distance" not in prompts[0]  # no vector signal invented
    assert f"id={neighbor.id}" in prompts[0]
    assert "Zeta Notes" not in prompts[0]  # non-matching tags never surface


@pytest.mark.es
async def test_soft_deleted_vector_neighbor_is_invisible(session_factory, seed_indexed):
    kotlin_id, python_id, jvm_id = await seed_three_docs(seed_indexed, PYTHON_CONTENT, JVM_CONTENT)
    await soft_delete(session_factory, python_id)
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(jvm_id)], prompts=prompts)
    )

    result = await service.associate_document(kotlin_id, tenant_id=DEFAULT_TENANT_ID)

    # The deleted Python doc's chunks still exist in PG but its rows are gone
    # from the candidates (live-doc join); the tag-leg JVM doc survives.
    assert "Python Notes" not in prompts[0]
    assert [item.document_id for item in result.associations] == [jvm_id]


@pytest.mark.es
async def test_soft_deleted_tag_neighbor_is_invisible(session_factory, seed_indexed):
    kotlin_id, python_id, jvm_id = await seed_three_docs(seed_indexed, PYTHON_CONTENT, JVM_CONTENT)
    await soft_delete(session_factory, jvm_id)
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(python_id)], prompts=prompts)
    )

    result = await service.associate_document(kotlin_id, tenant_id=DEFAULT_TENANT_ID)

    assert "JVM Internals" not in prompts[0]
    assert [item.document_id for item in result.associations] == [python_id]


async def test_vector_leg_keeps_min_distance_across_source_chunks(
    db_engine: AsyncEngine, db_session, session_factory
):
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    # Two source chunks (E0, E1); the neighbor sits at cosine distance 1 from
    # the first and 0 from the second — each document keeps its best distance.
    async with session_factory() as session:
        chunks_repo = DocumentChunkRepository(session)
        await chunks_repo.replace_for_document(
            source.id, ["chunk zero", "chunk one"], [basis_vector(0), basis_vector(1)]
        )
        await chunks_repo.replace_for_document(neighbor.id, ["neighbor body"], [basis_vector(1)])
        await session.commit()

    cosine_queries = 0

    def _count_cosine(conn, cursor, statement, parameters, context, executemany) -> None:
        nonlocal cosine_queries
        if "<=>" in statement:  # the pgvector cosine-distance operator
            cosine_queries += 1

    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(neighbor.id)], prompts=prompts)
    )
    event.listen(db_engine.sync_engine, "before_cursor_execute", _count_cosine)
    try:
        result = await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _count_cosine)

    # One indexed cosine query per source chunk: the query count is bounded
    # by the chunk count, and the min distance (0, from the E1 chunk) wins.
    assert cosine_queries == 2
    assert "cosine distance 0.0000" in result.associations[0].signal
    assert len(prompts) == 1


async def test_no_candidates_short_circuits_without_llm_call(db_session, session_factory):
    created = await make_document(db_session, ALPHA_SOURCE)  # unique tag, no chunks
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(uuid4())], prompts=prompts)
    )

    with capture_logs() as logs:
        result = await service.associate_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    assert result.associations == []
    assert result.document_id == created.id
    assert result.model == MODEL_NAME
    assert prompts == []  # zero model calls
    skipped = next(entry for entry in logs if entry["event"] == "agent_run_skipped")
    assert skipped["agent"] == "association"
    assert skipped["reason"] == "no_candidates"
    assert not any(entry["event"] == "agent_run_started" for entry in logs)


async def test_hallucinated_and_duplicate_ids_are_dropped(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    prompts: list[str] = []
    service = make_service(
        session_factory,
        scripted_association_model(
            [pick(neighbor.id), pick(uuid4()), pick(neighbor.id)], prompts=prompts
        ),
    )

    with capture_logs() as logs:
        result = await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)

    # Only the real candidate survives; the invented id and the repeat are
    # dropped, so every returned id came from the deterministic candidate set.
    assert [item.document_id for item in result.associations] == [neighbor.id]
    finished = next(entry for entry in logs if entry["event"] == "agent_run_finished")
    assert finished["selected_count"] == 1
    assert finished["dropped_count"] == 2


async def test_malformed_model_output_maps_to_clean_internal_error(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    await make_document(db_session, ALPHA_NEIGHBOR)
    service = make_service(
        session_factory,
        scripted_association_model([{"document_id": "not-a-uuid", "reason": "garbage"}]),
    )

    with capture_logs() as logs, pytest.raises(AppError) as exc_info:
        await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)

    # Structured-output validation retries inside the framework; once
    # exhausted the failure surfaces as the generic 500 envelope — no pydantic
    # traceback and no payload leakage to the client, details in logs only.
    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "internal_error"
    assert "not-a-uuid" not in exc_info.value.message
    failed = next(entry for entry in logs if entry["event"] == "agent_run_failed")
    assert failed["agent"] == "association"
    assert failed["error_class"] == "UnexpectedModelBehavior"
    assert not any(entry["event"] == "agent_run_finished" for entry in logs)


async def test_missing_document_raises_not_found_before_any_model_call(session_factory):
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(uuid4())], prompts=prompts)
    )

    with pytest.raises(NotFoundError):
        await service.associate_document(uuid4(), tenant_id=DEFAULT_TENANT_ID)

    assert prompts == []


async def test_soft_deleted_source_raises_not_found(db_session, session_factory):
    created = await make_document(db_session, ALPHA_SOURCE)
    await DocumentService(db_session).delete_document(created.id, tenant_id=DEFAULT_TENANT_ID)
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(uuid4())], prompts=prompts)
    )

    with pytest.raises(NotFoundError):
        await service.associate_document(created.id, tenant_id=DEFAULT_TENANT_ID)

    assert prompts == []


async def test_run_lifecycle_logged_without_reasons_or_titles(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    service = make_service(
        session_factory,
        scripted_association_model([pick(neighbor.id, "Sensitive curated reason.")]),
    )

    with capture_logs() as logs:
        await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)

    started = next(entry for entry in logs if entry["event"] == "agent_run_started")
    finished = next(entry for entry in logs if entry["event"] == "agent_run_finished")
    assert started["agent"] == "association"
    assert started["candidate_count"] == 1
    assert started["run_id"]
    assert finished["agent"] == "association"
    assert finished["outcome"] == "success"
    assert finished["model"] == MODEL_NAME
    assert finished["selected_count"] == 1
    assert finished["dropped_count"] == 0
    assert finished["input_tokens"] > 0
    assert all(entry.get("document_id") == str(source.id) for entry in logs)
    # Reasons and candidate titles are user data: never logged at any level.
    assert "Sensitive curated reason." not in str(logs)
    assert "Alpha Companion" not in str(logs)


async def test_provider_failure_wraps_into_llm_provider_error_and_logs_failure(
    db_session, session_factory
):
    source = await make_document(db_session, ALPHA_SOURCE)
    await make_document(db_session, ALPHA_NEIGHBOR)
    service = make_service(
        session_factory,
        scripted_association_model(
            [],
            fail=openai.APIStatusError(
                "upstream exploded with secret detail",
                response=httpx.Response(
                    500, request=httpx.Request("POST", "http://provider.test/v1/chat")
                ),
                body=None,
            ),
        ),
    )

    with capture_logs() as logs, pytest.raises(LLMProviderError) as exc_info:
        await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)

    # Provider internals stay out of the response; they went to logs only.
    assert "upstream exploded" not in exc_info.value.message
    failed = next(entry for entry in logs if entry["event"] == "agent_run_failed")
    assert failed["agent"] == "association"
    assert failed["outcome"] == "llm_provider_error"
    assert failed["error_class"] == "APIStatusError"
    assert not any(entry["event"] == "agent_run_finished" for entry in logs)


# --- stream contract (design: agent stream) ---


async def drain(events):
    """Collect a typed event stream into a list."""
    return [event async for event in events]


async def test_stream_success_emits_atomic_result_and_done(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    service = make_service(
        session_factory, scripted_association_model([pick(neighbor.id, "Streamed reason.")])
    )

    events = await drain(service.associate_document_stream(source.id, tenant_id=DEFAULT_TENANT_ID))

    # Atomic structured output: no partial association events, no progress.
    assert [type(event) for event in events] == [
        AgentRunStartedEvent,
        AssociationsResultEvent,
        AgentDoneEvent,
    ]
    run_started = events[0]
    assert run_started.kind == "associations"
    assert run_started.document_id == source.id
    assert run_started.run_id
    result = events[1]
    assert [item.document_id for item in result.associations] == [neighbor.id]
    assert result.associations[0].reason == "Streamed reason."
    assert result.model == MODEL_NAME
    assert result.latency_ms >= 0
    done = events[2]
    assert done.run_id == run_started.run_id
    assert done.outcome == "success"
    assert done.latency_ms == result.latency_ms


async def test_stream_cache_hit_skips_model_call(db_session, session_factory):
    prompts: list[str] = []
    cache = FakeCache()
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    warming = AssociationService(
        scripted_association_model([pick(neighbor.id)], prompts=prompts),
        MODEL_NAME,
        session_factory=session_factory,
        cache=cache,
        cache_ttl_seconds=600,
    )
    await warming.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)

    events = await drain(warming.associate_document_stream(source.id, tenant_id=DEFAULT_TENANT_ID))

    assert [type(event) for event in events] == [
        AgentRunStartedEvent,
        AssociationsResultEvent,
        AgentDoneEvent,
    ]
    assert [item.document_id for item in events[1].associations] == [neighbor.id]
    assert len(prompts) == 1  # the warm run only; the hit made no model call


async def test_stream_no_candidates_returns_empty_result_without_llm_call(
    db_session, session_factory
):
    created = await make_document(db_session, ALPHA_SOURCE)  # unique tag, no chunks
    prompts: list[str] = []
    service = make_service(
        session_factory, scripted_association_model([pick(uuid4())], prompts=prompts)
    )

    events = await drain(service.associate_document_stream(created.id, tenant_id=DEFAULT_TENANT_ID))

    assert [type(event) for event in events] == [
        AgentRunStartedEvent,
        AssociationsResultEvent,
        AgentDoneEvent,
    ]
    assert events[1].associations == []
    assert prompts == []  # zero model calls


async def test_stream_provider_failure_emits_single_terminal_error(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    await make_document(db_session, ALPHA_NEIGHBOR)
    service = make_service(
        session_factory,
        scripted_association_model(
            [],
            fail=openai.APIStatusError(
                "upstream exploded with secret detail",
                response=httpx.Response(
                    500, request=httpx.Request("POST", "http://provider.test/v1/chat")
                ),
                body=None,
            ),
        ),
    )

    with capture_logs() as logs:
        events = await drain(
            service.associate_document_stream(source.id, tenant_id=DEFAULT_TENANT_ID)
        )

    # run_started stands, then exactly one terminal error and no done.
    assert [type(event) for event in events] == [AgentRunStartedEvent, ErrorEvent]
    assert events[-1].code == "llm_provider_error"
    assert "upstream exploded" not in events[-1].message
    assert not any(entry["event"] == "agent_run_finished" for entry in logs)


async def test_stream_missing_document_raises_before_first_event(session_factory):
    service = make_service(session_factory, scripted_association_model([pick(uuid4())]))

    with pytest.raises(NotFoundError):
        await drain(service.associate_document_stream(uuid4(), tenant_id=DEFAULT_TENANT_ID))


async def test_wrapper_result_matches_stream_result_event(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    service = make_service(session_factory, scripted_association_model([pick(neighbor.id)]))

    wrapper_result = await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)
    stream_events = await drain(
        service.associate_document_stream(source.id, tenant_id=DEFAULT_TENANT_ID)
    )
    stream_result = next(e for e in stream_events if isinstance(e, AssociationsResultEvent))

    assert wrapper_result.model_dump(exclude={"latency_ms"}) == stream_result.model_dump(
        exclude={"latency_ms"}
    )
