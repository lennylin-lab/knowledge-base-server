"""Search API contract: status codes, envelope, mode field, degradation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from elasticsearch import AsyncElasticsearch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.api.deps import (
    build_search_service,
    embedding_provider_from_settings,
    get_search_service,
)
from app.core.exceptions import LLMProviderError
from app.rag.retriever import Retriever
from app.services.search import SearchService
from corpus import KOTLIN_SECTION, neighbor_scripted_provider, seed_corpus
from fakes import ScriptedEmbeddingProvider, hermetic_settings

ITEM_FIELDS = {
    "document_id",
    "document_title",
    "document_tags",
    "chunk_index",
    "content",
    "score",
    "es_rank",
    "vector_rank",
}


@dataclass
class SearchWorld:
    """Seeded corpus plus the handles needed to rewire or break the search."""

    provider: ScriptedEmbeddingProvider
    app: FastAPI
    session_factory: async_sessionmaker[AsyncSession]
    es_client: AsyncElasticsearch
    es_index_name: str


@pytest.fixture
async def hybrid_world(app, seed_indexed, session_factory, es_client, es_index_name) -> SearchWorld:
    """Corpus seeded with a scripted provider; the BM25 query rides E0 too."""
    provider = neighbor_scripted_provider()
    provider.vectors["zorblat"] = provider.vectors[KOTLIN_SECTION]
    await seed_corpus(seed_indexed, provider)
    return SearchWorld(provider, app, session_factory, es_client, es_index_name)


def _override_search(world: SearchWorld, service: SearchService) -> None:
    world.app.dependency_overrides[get_search_service] = lambda: service


@pytest.fixture
async def search_client(hybrid_world: SearchWorld) -> AsyncIterator[AsyncClient]:
    """ASGI client with the hybrid retriever wired onto test infra."""
    service = SearchService(
        Retriever(
            session_factory=hybrid_world.session_factory,
            es_client=hybrid_world.es_client,
            embedding_provider=hybrid_world.provider,
            es_index=hybrid_world.es_index_name,
        )
    )
    _override_search(hybrid_world, service)
    transport = ASGITransport(app=hybrid_world.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    hybrid_world.app.dependency_overrides.clear()


@pytest.fixture
async def bm25_search_client(
    app, seed_indexed, session_factory, es_client, es_index_name
) -> AsyncIterator[AsyncClient]:
    """Retriever without an embedding provider: the no-API-key wiring."""
    await seed_corpus(seed_indexed, ScriptedEmbeddingProvider())
    service = SearchService(
        Retriever(
            session_factory=session_factory,
            es_client=es_client,
            embedding_provider=None,
            es_index=es_index_name,
        )
    )
    app.dependency_overrides[get_search_service] = lambda: service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.db
@pytest.mark.es
async def test_search_returns_200_with_hybrid_mode_and_full_hit_shape(search_client):
    resp = await search_client.get("/api/v1/search", params={"q": "zorblat"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "hybrid"
    items = body["items"]
    assert len(items) == 2  # ES: kotlin only; vector: kotlin + python

    top = items[0]
    assert set(top) == ITEM_FIELDS
    assert top["document_title"] == "Kotlin Notes"
    assert top["document_tags"] == ["kotlin"]
    assert top["chunk_index"] == 0
    assert top["content"] == KOTLIN_SECTION
    assert top["score"] > 0
    assert top["es_rank"] == 1
    assert top["vector_rank"] == 1
    assert items[1]["document_title"] == "Python Notes"
    assert items[1]["es_rank"] is None
    assert items[1]["vector_rank"] == 2


@pytest.mark.db
@pytest.mark.es
async def test_search_tag_filter_is_normalized_and_narrows_results(search_client):
    resp = await search_client.get("/api/v1/search", params={"q": "notes", "tag": "  Kotlin  "})

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["document_title"] for item in items] == ["Kotlin Notes"]
    assert items[0]["document_tags"] == ["kotlin"]


@pytest.mark.db
@pytest.mark.es
async def test_search_responds_bm25_when_no_api_key_wired(bm25_search_client):
    resp = await bm25_search_client.get("/api/v1/search", params={"q": "zorblat"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "bm25"
    assert body["items"][0]["document_title"] == "Kotlin Notes"
    assert body["items"][0]["vector_rank"] is None


@pytest.mark.db
@pytest.mark.es
async def test_provider_failure_mid_search_degrades_to_bm25(search_client, hybrid_world):
    hybrid_world.provider.error = LLMProviderError("provider down")

    resp = await search_client.get("/api/v1/search", params={"q": "zorblat"})

    assert resp.status_code == 200  # never a 5xx over a missing vector leg
    body = resp.json()
    assert body["mode"] == "bm25"
    assert body["items"][0]["document_title"] == "Kotlin Notes"
    assert body["items"][0]["vector_rank"] is None


@pytest.mark.db
@pytest.mark.es
async def test_es_failure_maps_to_502_envelope(app, session_factory, es_client, es_index_name):
    service = SearchService(
        Retriever(
            session_factory=session_factory,
            es_client=es_client,
            embedding_provider=None,
            es_index=f"{es_index_name}-never-created",
        )
    )
    app.dependency_overrides[get_search_service] = lambda: service
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.get("/api/v1/search", params={"q": "anything"})

    assert resp.status_code == 502
    error = resp.json()["error"]
    assert error["code"] == "search_index_error"
    assert error["details"]["operation"] == "search_chunks"
    app.dependency_overrides.clear()


# --- validation contract (offline: rejects before any dependency work) ---


async def test_missing_q_returns_422_envelope(client):
    resp = await client.get("/api/v1/search")

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["message"]


async def test_empty_q_returns_422_envelope(client):
    resp = await client.get("/api/v1/search", params={"q": ""})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_limit_zero_returns_422_envelope(client):
    resp = await client.get("/api/v1/search", params={"q": "x", "limit": 0})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_limit_above_max_returns_422_envelope(client):
    resp = await client.get("/api/v1/search", params={"q": "x", "limit": 51})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


# --- no-key wiring (offline: construction only, no provider calls) ---


async def test_embedding_provider_from_settings_returns_none_without_key():
    assert (
        embedding_provider_from_settings(hermetic_settings(EMBEDDING_API_KEY=SecretStr(""))) is None
    )
    assert (
        embedding_provider_from_settings(hermetic_settings(EMBEDDING_API_KEY=SecretStr("k")))
        is not None
    )


async def test_build_search_service_without_key_warns_vector_search_disabled():
    with capture_logs() as logs:
        build_search_service(hermetic_settings(EMBEDDING_API_KEY=SecretStr("")))

    degraded = [entry for entry in logs if entry["event"] == "vector_search_disabled"]
    assert len(degraded) == 1
    assert degraded[0]["log_level"] == "warning"


async def test_build_search_service_with_key_does_not_warn():
    with capture_logs() as logs:
        build_search_service(hermetic_settings(EMBEDDING_API_KEY=SecretStr("test-key")))

    assert not [entry for entry in logs if entry["event"] == "vector_search_disabled"]
