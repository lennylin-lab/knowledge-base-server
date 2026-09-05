"""Writing API contract (offline): SSE wire format, validation, 503 wiring.

The writing dependency is overridden with a FunctionModel-backed service, so
no request here can reach a real provider. The unconfigured case drives the
REAL `build_writing_service` to prove the key check fires before any stream.
`parse_sse` comes from `fakes`: one parser for the one event vocabulary both
streaming endpoints share.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.deps import build_writing_service, get_writing_service
from app.rag.retriever import SearchOutcome
from app.schemas.writing import DRAFT_MAX_CHARS
from app.services.agents import WritingService
from fakes import StubRetriever, hermetic_settings, parse_sse, retrieved_chunk, scripted_chat_model

DRAFT = "# Draft heading\n\nSome draft body about zorblat."


def _stub_service() -> WritingService:
    return WritingService(
        StubRetriever(
            outcome=SearchOutcome(
                mode="hybrid",
                items=[retrieved_chunk(content="zorblat everywhere", document_title="Notes")],
                es_hits=1,
                vector_hits=1,
            )
        ),
        scripted_chat_model(
            tool_calls=["zorblat"],
            answer_parts=["Zorblat appears in the notes ", "and is worth citing [1]."],
        ),
        mode="hybrid",
    )


@pytest.fixture
async def writing_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGI client with a scripted, offline writing service behind the endpoint."""
    app.dependency_overrides[get_writing_service] = _stub_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _post(ac: AsyncClient, payload: object) -> httpx.Response:
    return await ac.post("/api/v1/writing/suggest", json=payload)


async def test_suggest_streams_sse_content_type_and_event_sequence(writing_client):
    resp = await _post(writing_client, {"draft": DRAFT})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-request-id"]

    events = parse_sse(resp.text)
    assert [name for name, _ in events] == [
        "run_started",
        "sources",
        "answer_delta",
        "answer_delta",
        "done",
    ]
    run_started = events[0][1]
    assert run_started["run_id"]
    assert run_started["mode"] == "hybrid"

    sources = events[1][1]
    assert set(sources["items"][0]) == {
        "document_id",
        "document_title",
        "document_tags",
        "chunk_index",
        "content",
        "score",
        "es_rank",
        "vector_rank",
        "es_score",
        "vector_distance",
    }

    answer = "".join(data["text"] for name, data in events if name == "answer_delta")
    assert answer == "Zorblat appears in the notes and is worth citing [1]."

    done = events[-1][1]
    assert done["run_id"] == run_started["run_id"]
    assert done["outcome"] == "success"
    assert done["tool_calls"] == 1
    assert done["latency_ms"] >= 0


async def test_zero_tool_run_over_sse_has_no_sources_event(app):
    service = WritingService(
        StubRetriever(),
        scripted_chat_model(answer_parts=["No retrieval needed."]),
        mode="bm25",
    )
    app.dependency_overrides[get_writing_service] = lambda: service
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await _post(ac, {"draft": DRAFT})

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert [name for name, _ in events] == ["run_started", "answer_delta", "done"]
    assert events[0][1]["mode"] == "bm25"
    assert events[-1][1]["tool_calls"] == 0
    app.dependency_overrides.clear()


async def test_instruction_field_is_accepted(writing_client):
    resp = await _post(writing_client, {"draft": DRAFT, "instruction": "critique"})

    assert resp.status_code == 200
    assert parse_sse(resp.text)


@pytest.mark.parametrize("limit", [1, 20])
async def test_limit_boundaries_are_accepted(writing_client, limit):
    resp = await _post(writing_client, {"draft": DRAFT, "limit": limit})

    assert resp.status_code == 200


# --- validation contract (offline) ---
#
# The dependency is stubbed for these: FastAPI resolves dependencies before
# reporting body-validation errors, so an unconfigured deployment answers
# 503 rather than 422 for an invalid body (pinned below). The request-schema
# contract itself is what these tests pin down.


async def test_empty_draft_returns_422_envelope(writing_client):
    resp = await _post(writing_client, {"draft": ""})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["message"]


async def test_missing_draft_returns_422_envelope(writing_client):
    resp = await _post(writing_client, {})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_non_string_draft_returns_422_envelope(writing_client):
    resp = await _post(writing_client, {"draft": 42})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_oversized_draft_returns_422_envelope(writing_client):
    resp = await _post(writing_client, {"draft": "x" * (DRAFT_MAX_CHARS + 1)})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_max_length_draft_is_accepted(writing_client):
    resp = await _post(writing_client, {"draft": "x" * DRAFT_MAX_CHARS})

    assert resp.status_code == 200


@pytest.mark.parametrize("limit", [0, 21, -1])
async def test_out_of_range_limit_returns_422_envelope(writing_client, limit):
    resp = await _post(writing_client, {"draft": DRAFT, "limit": limit})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


# --- no-key gate (drives the REAL constructor) ---


async def test_unconfigured_writing_returns_503_envelope_before_any_stream(app):
    app.dependency_overrides[get_writing_service] = lambda: build_writing_service(
        hermetic_settings(CHAT_API_KEY=SecretStr(""))
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await _post(ac, {"draft": DRAFT})

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    error: dict[str, Any] = resp.json()["error"]
    assert error["code"] == "chat_unavailable"
    assert error["message"]
    app.dependency_overrides.clear()


async def test_invalid_body_on_unconfigured_writing_still_returns_503(app):
    # Pins the documented ordering (see `build_chat_service`): the no-key
    # gate wins over request-schema errors, like any auth-style dependency.
    app.dependency_overrides[get_writing_service] = lambda: build_writing_service(
        hermetic_settings(CHAT_API_KEY=SecretStr(""))
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await _post(ac, {"draft": ""})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "chat_unavailable"
    app.dependency_overrides.clear()


async def test_build_writing_service_with_key_returns_service():
    service = build_writing_service(hermetic_settings(CHAT_API_KEY=SecretStr("test-key")))

    assert isinstance(service, WritingService)


async def test_build_writing_service_mode_tracks_the_embedding_wiring():
    # run_started.mode must tell the truth about the retriever actually built
    # (the documented deviation from chat's fixed hybrid): hybrid only when an
    # embedding key exists, BM25-only otherwise. `_mode` is read directly —
    # the constructor wires a real model, so the stream cannot run offline.
    bm25_only = build_writing_service(hermetic_settings(CHAT_API_KEY=SecretStr("k")))
    hybrid = build_writing_service(
        hermetic_settings(CHAT_API_KEY=SecretStr("k"), EMBEDDING_API_KEY=SecretStr("e"))
    )

    assert bm25_only._mode == "bm25"
    assert hybrid._mode == "hybrid"
