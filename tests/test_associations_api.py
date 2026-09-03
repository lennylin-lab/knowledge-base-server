"""Associations API contract (offline): 200 shape, 404 envelope, 503 no-key gate.

The association dependency is overridden with a FunctionModel-backed service
over the test database, so no request here can reach a real provider. The
200-path corpus is seeded exactly like production data (service write ->
repository chunk/embedding insert), so the endpoint's deterministic candidate
gathering runs for real — the model only picks from what it was given.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import build_association_service, get_association_service
from app.repositories.document_chunk import DocumentChunkRepository
from app.services.agents import AssociationService
from fakes import basis_vector, hermetic_settings, scripted_association_model

MODEL_NAME = "test-chat-model"
MISSING_ID = "00000000-0000-0000-0000-000000000000"

KOTLIN_DOC = "---\ntitle: Kotlin Notes\ntags: [kotlin]\n---\n\n# Kotlin notes\n"
PYTHON_DOC = "---\ntitle: Python Notes\ntags: [python]\n---\n\n# Python notes\n"


@pytest.fixture
def install_scripted_association(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[[list[dict[str, str]]], list[str]]:
    """Swap the association dependency for a scripted offline service.

    Returns an installer whose result is the shared prompt recorder — what
    actually reached the model, in order (its length is the model-call count).
    """
    prompts: list[str] = []

    def _install(picks: list[dict[str, str]]) -> list[str]:
        app.dependency_overrides[get_association_service] = lambda: AssociationService(
            scripted_association_model(picks, prompts=prompts),
            MODEL_NAME,
            session_factory=session_factory,
        )
        return prompts

    return _install


async def seed_chunk(
    session_factory: async_sessionmaker[AsyncSession],
    document_id: UUID,
    content: str,
    vector: list[float],
) -> None:
    """Insert one chunk + embedding directly (no ES/pipeline needed)."""
    async with session_factory() as session:
        await DocumentChunkRepository(session).replace_for_document(
            document_id, [content], [vector]
        )
        await session.commit()


@pytest.mark.db
async def test_associations_returns_200_with_candidate_metadata_only(
    db_client, session_factory, install_scripted_association
):
    kotlin = (await db_client.post("/api/v1/documents", json={"content": KOTLIN_DOC})).json()
    python = (await db_client.post("/api/v1/documents", json={"content": PYTHON_DOC})).json()
    # Scripted vectors: the Kotlin chunk and the Python chunk are orthogonal,
    # so the vector leg deterministically surfaces Python from Kotlin.
    await seed_chunk(session_factory, UUID(kotlin["id"]), "Kotlin body", basis_vector(0))
    await seed_chunk(session_factory, UUID(python["id"]), "Python body", basis_vector(1))
    hallucinated = uuid4()
    prompts = install_scripted_association(
        [
            {"document_id": python["id"], "reason": "Neighboring language notes."},
            {"document_id": str(hallucinated), "reason": "Invented document."},
        ]
    )

    resp = await db_client.post(f"/api/v1/documents/{kotlin['id']}/associations")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"document_id", "associations", "model", "latency_ms"}
    assert body["document_id"] == kotlin["id"]
    assert body["model"] == MODEL_NAME
    assert body["latency_ms"] >= 0
    # The hallucinated id is gone: every returned id was a real candidate and
    # every field but the reason is deterministic candidate metadata.
    assert len(body["associations"]) == 1
    item = body["associations"][0]
    assert set(item) == {"document_id", "title", "tags", "reason", "signal"}
    assert item["document_id"] == python["id"]
    assert item["title"] == "Python Notes"
    assert item["tags"] == ["python"]
    assert item["reason"] == "Neighboring language notes."
    assert "cosine distance" in item["signal"]
    assert len(prompts) == 1  # exactly one model call


@pytest.mark.db
async def test_associations_of_missing_document_returns_404_envelope(
    db_client, install_scripted_association
):
    prompts = install_scripted_association([{"document_id": MISSING_ID, "reason": "never"}])

    resp = await db_client.post(f"/api/v1/documents/{MISSING_ID}/associations")

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"]
    assert prompts == []  # nothing reached the model


@pytest.mark.db
async def test_associations_of_soft_deleted_document_returns_404_envelope(
    db_client, install_scripted_association
):
    prompts = install_scripted_association([{"document_id": MISSING_ID, "reason": "never"}])
    created = (await db_client.post("/api/v1/documents", json={"content": KOTLIN_DOC})).json()
    await db_client.delete(f"/api/v1/documents/{created['id']}")

    resp = await db_client.post(f"/api/v1/documents/{created['id']}/associations")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert prompts == []


async def test_unconfigured_associations_returns_503_envelope_before_any_llm_call(app):
    # The real constructor, not a stub: the key check must fire here. It
    # raises chat's error on purpose — one no-LLM-fallback gate, one code.
    app.dependency_overrides[get_association_service] = lambda: build_association_service(
        hermetic_settings(CHAT_API_KEY=SecretStr(""))
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(f"/api/v1/documents/{MISSING_ID}/associations")

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    error = resp.json()["error"]
    assert error["code"] == "chat_unavailable"
    assert error["message"]
    app.dependency_overrides.clear()


async def test_build_association_service_with_key_returns_service():
    service = build_association_service(hermetic_settings(CHAT_API_KEY=SecretStr("test-key")))

    assert isinstance(service, AssociationService)
