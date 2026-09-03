"""Summary API contract (offline): 200 shape, 404 envelope, 503 no-key gate.

The summarize dependency is overridden with a FunctionModel-backed service,
so no request here can reach a real provider. The unconfigured case drives
the REAL `build_summarize_service` to prove the key check fires before any
document load or model call.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import build_summarize_service, get_summarize_service
from app.services.agents import SummarizeService
from fakes import hermetic_settings, scripted_summarize_model

# Stands in for Settings.CHAT_MODEL at wiring time; deps.py passes that
# setting into the service, and the response must echo it back.
MODEL_NAME = "test-chat-model"
MISSING_ID = "00000000-0000-0000-0000-000000000000"
FM_DOC = "---\ntitle: Contract Note\ntags: [api]\n---\n\n# Body\n"


def long_content(sections: int = 3) -> str:
    """Same construction as test_summarize_service: >1 chunk guaranteed."""
    body = "The quibnard decision changed everything for the zorblat team. " * 18
    return "\n\n".join(f"# Section {i}\n\n{body}" for i in range(1, sections + 1))


@pytest.fixture
def install_scripted_summary(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[[list[str]], list[str]]:
    """Swap the summarize dependency for a scripted offline service.

    Returns an installer whose result is the shared prompt recorder — what
    actually reached the model, in order.
    """
    prompts: list[str] = []

    def _install(outputs: list[str]) -> list[str]:
        app.dependency_overrides[get_summarize_service] = lambda: SummarizeService(
            scripted_summarize_model(outputs, prompts=prompts),
            MODEL_NAME,
            session_factory=session_factory,
        )
        return prompts

    return _install


@pytest.mark.db
async def test_summary_returns_200_with_scripted_result(db_client, install_scripted_summary):
    prompts = install_scripted_summary(["Contract summary."])
    created = (await db_client.post("/api/v1/documents", json={"content": FM_DOC})).json()

    resp = await db_client.post(f"/api/v1/documents/{created['id']}/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"document_id", "summary", "model", "latency_ms"}
    assert body["document_id"] == created["id"]
    assert body["summary"] == "Contract summary."
    assert body["model"] == MODEL_NAME
    assert body["latency_ms"] >= 0
    assert len(prompts) == 1  # short doc: exactly one model pass


@pytest.mark.db
async def test_summary_of_long_document_returns_the_combine_output(
    db_client, install_scripted_summary
):
    prompts = install_scripted_summary(["s-one", "s-two", "s-three", "Final reduce."])
    created = (
        await db_client.post(
            "/api/v1/documents",
            json={"content": f"---\ntitle: Long Note\n---\n\n{long_content(3)}"},
        )
    ).json()

    resp = await db_client.post(f"/api/v1/documents/{created['id']}/summary")

    assert resp.status_code == 200
    assert resp.json()["summary"] == "Final reduce."
    assert len(prompts) == 4  # 3 chunk passes + 1 combine through the endpoint


@pytest.mark.db
async def test_summary_of_missing_document_returns_404_envelope(
    db_client, install_scripted_summary
):
    prompts = install_scripted_summary(["never"])

    resp = await db_client.post(f"/api/v1/documents/{MISSING_ID}/summary")

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"]
    assert prompts == []  # nothing reached the model


@pytest.mark.db
async def test_summary_of_soft_deleted_document_returns_404_envelope(
    db_client, install_scripted_summary
):
    prompts = install_scripted_summary(["never"])
    created = (await db_client.post("/api/v1/documents", json={"content": FM_DOC})).json()
    await db_client.delete(f"/api/v1/documents/{created['id']}")

    resp = await db_client.post(f"/api/v1/documents/{created['id']}/summary")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert prompts == []


async def test_unconfigured_summary_returns_503_envelope_before_any_llm_call(app):
    # The real constructor, not a stub: the key check must fire here. It
    # raises chat's error on purpose — one no-LLM-fallback gate, one code.
    app.dependency_overrides[get_summarize_service] = lambda: build_summarize_service(
        hermetic_settings(CHAT_API_KEY=SecretStr(""))
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(f"/api/v1/documents/{MISSING_ID}/summary")

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    error = resp.json()["error"]
    assert error["code"] == "chat_unavailable"
    assert error["message"]
    app.dependency_overrides.clear()


async def test_build_summarize_service_with_key_returns_service():
    service = build_summarize_service(hermetic_settings(CHAT_API_KEY=SecretStr("test-key")))

    assert isinstance(service, SummarizeService)
