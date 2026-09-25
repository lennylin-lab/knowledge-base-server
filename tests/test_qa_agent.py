"""QA agent integration: the `search_knowledge` tool against real retrieval.

The model is still a scripted FunctionModel (no live LLM), but the tool runs
the real hybrid retriever over the seeded corpus, so this is the proof that
grounding, sources, and citations line up end to end. Requires db + es
(auto-skipped when either is unreachable).
"""

from __future__ import annotations

import pytest
from elasticsearch import AsyncElasticsearch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import build_chat_service
from app.core.config import get_settings
from app.models.tenant import DEFAULT_TENANT_ID
from app.rag.retriever import Retriever
from app.schemas.chat import AnswerDeltaEvent, DoneEvent, RunStartedEvent, SourcesEvent
from app.services.chat import ChatService
from corpus import KOTLIN_CONTENT, KOTLIN_SECTION, neighbor_scripted_provider, seed_corpus
from fakes import GATES_OFF, scripted_chat_model

pytestmark = [pytest.mark.db, pytest.mark.es]


async def test_tool_retrieves_seeded_corpus_and_answer_cites_it(
    seed_indexed,
    session_factory: async_sessionmaker[AsyncSession],
    es_client: AsyncElasticsearch,
    es_index_name: str,
):
    kotlin_id, python_id = await seed_corpus(seed_indexed, neighbor_scripted_provider())
    retriever = Retriever(
        session_factory=session_factory,
        es_client=es_client,
        embedding_provider=None,  # BM25-only wiring; matches the service mode below
        es_index=es_index_name,
    )
    service = ChatService(
        retriever,
        scripted_chat_model(
            tool_calls=["zorblat"],
            answer_parts=["Zorblat is covered in the Kotlin notes [1]."],
        ),
        mode="bm25",
    )

    events = [
        event
        async for event in service.ask("What is zorblat?", limit=8, tenant_id=DEFAULT_TENANT_ID)
    ]

    kinds = [type(event).__name__ for event in events]
    assert kinds[0] == "RunStartedEvent"
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].mode == "bm25"
    assert kinds[-1] == "DoneEvent"

    # "zorblat" exists only in the Kotlin document: exactly that source.
    sources = [event for event in events if isinstance(event, SourcesEvent)]
    assert len(sources) == 1
    items = sources[0].items
    assert [item.document_id for item in items] == [kotlin_id]
    assert python_id not in {item.document_id for item in items}
    assert items[0].document_title == "Kotlin Notes"
    assert items[0].document_tags == ["kotlin"]
    assert items[0].content == KOTLIN_SECTION
    assert items[0].chunk_index == 0
    assert items[0].es_rank == 1
    assert items[0].vector_rank is None  # BM25-only retriever

    # The bracketed citation refers to the streamed source above it.
    answer = "".join(event.text for event in events if isinstance(event, AnswerDeltaEvent))
    assert answer == "Zorblat is covered in the Kotlin notes [1]."

    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.outcome == "success"
    assert done.tool_calls == 1


async def test_run_id_is_echoed_across_the_stream(
    seed_indexed,
    session_factory: async_sessionmaker[AsyncSession],
    es_client: AsyncElasticsearch,
    es_index_name: str,
):
    await seed_corpus(seed_indexed, neighbor_scripted_provider())
    service = ChatService(
        Retriever(
            session_factory=session_factory,
            es_client=es_client,
            embedding_provider=None,
            es_index=es_index_name,
        ),
        scripted_chat_model(answer_parts=["No retrieval needed."]),
        mode="bm25",
    )

    events = [event async for event in service.ask("Anything?", tenant_id=DEFAULT_TENANT_ID)]

    run_ids = {event.run_id for event in events if isinstance(event, (RunStartedEvent, DoneEvent))}
    assert len(run_ids) == 1
    assert run_ids.pop()


# --- context-block Summary line (the document's front-matter blurb) ---


async def test_context_blocks_render_summary_line_only_when_description_present(
    seed_indexed,
    session_factory: async_sessionmaker[AsyncSession],
    es_client: AsyncElasticsearch,
    es_index_name: str,
):
    """Both branches pinned through the real tool: a described document's
    block carries `Summary: {blurb}` under the header; description-less
    blocks stay byte-identical to the pre-description shape (no line, no
    blank-line drift)."""
    provider = neighbor_scripted_provider()
    described = (
        "---\ntitle: Described Notes\ntags: [described]\n"
        "description: A hand-written blurb.\n---\n\n"
        f"# Described\n\n{('zorblat ' * 130).strip()}"
    )
    await seed_indexed(provider, KOTLIN_CONTENT)  # no description
    await seed_indexed(provider, described)
    tool_results: list[str] = []
    service = ChatService(
        Retriever(
            session_factory=session_factory,
            es_client=es_client,
            embedding_provider=None,
            es_index=es_index_name,
            **GATES_OFF,
        ),
        scripted_chat_model(
            tool_calls=["zorblat"], answer_parts=["[1]."], tool_results=tool_results
        ),
        mode="bm25",
    )

    events = [
        event
        async for event in service.ask("What is zorblat?", limit=8, tenant_id=DEFAULT_TENANT_ID)
    ]

    assert len(events) >= 1
    tool_text = tool_results[0]
    blocks = tool_text.split("\n\n")
    described_block = next(block for block in blocks if "Described Notes" in block)
    plain_block = next(block for block in blocks if "Kotlin Notes" in block)
    assert (
        "Described Notes (chunk 0; tags: described)\nSummary: A hand-written blurb.\n"
        in described_block
    )
    # The empty branch collapses: header line directly followed by content.
    assert plain_block.startswith("[1] Kotlin Notes (chunk 0; tags: kotlin)\n")
    assert "Summary:" not in plain_block
    # The described blurb also reaches the streamed source item.
    sources = [event for event in events if isinstance(event, SourcesEvent)]
    by_title = {item.document_title: item for item in sources[0].items}
    assert by_title["Described Notes"].document_description == "A hand-written blurb."
    assert by_title["Kotlin Notes"].document_description == ""


# --- live provider smoke (deselected by default: -m "not live_llm") ---


@pytest.mark.live_llm
@pytest.mark.db
@pytest.mark.es
async def test_live_provider_streams_a_complete_run():
    """One real question through the production wiring (manual/CI opt-in)."""
    settings = get_settings()
    if not settings.CHAT_API_KEY.get_secret_value():
        pytest.skip("CHAT_API_KEY not configured")
    service = build_chat_service(settings)

    events = [
        event
        async for event in service.ask("What is a zorblat?", limit=3, tenant_id=DEFAULT_TENANT_ID)
    ]

    kinds = [type(event).__name__ for event in events]
    assert kinds[0] == "RunStartedEvent"
    assert kinds[-1] == "DoneEvent"
