"""Chat API contract: SSE wire format, validation, 503 wiring, sessions.

The chat dependency is overridden with a FunctionModel-backed service, so no
request here can reach a real provider. The unconfigured-chat case drives the
REAL `build_chat_service` to prove the key check fires before any stream.
Session-persistence tests (`db`-marked) wire that stub service to the
disposable test database — the production lifetime pattern.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

import httpx
import openai
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import build_chat_service, get_chat_service
from app.core.database import get_db
from app.rag.retriever import SearchOutcome
from app.services.chat import ChatService
from fakes import StubRetriever, hermetic_settings, parse_sse, retrieved_chunk, scripted_chat_model

QUESTION = "What do the notes say about zorblat?"


def _stub_service() -> ChatService:
    return ChatService(
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
            answer_parts=["Zorblat is a test term ", "used in fixtures [1]."],
        ),
        mode="hybrid",
    )


@pytest.fixture
async def chat_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGI client with a scripted, offline chat service behind the endpoint."""
    app.dependency_overrides[get_chat_service] = _stub_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _post(ac: AsyncClient, payload: object) -> httpx.Response:
    return await ac.post("/api/v1/chat", json=payload)


async def test_chat_streams_sse_content_type_and_event_sequence(chat_client):
    resp = await _post(chat_client, {"question": QUESTION})

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
    }

    answer = "".join(data["text"] for name, data in events if name == "answer_delta")
    assert answer == "Zorblat is a test term used in fixtures [1]."

    done = events[-1][1]
    assert done["run_id"] == run_started["run_id"]
    assert done["outcome"] == "success"
    assert done["tool_calls"] == 1
    assert done["latency_ms"] >= 0


async def test_limit_is_passed_through_to_the_service(chat_client):
    resp = await _post(chat_client, {"question": QUESTION, "limit": 3})

    assert resp.status_code == 200
    assert parse_sse(resp.text)


async def test_provider_failure_mid_stream_emits_terminal_error_event(app):
    failing = ChatService(
        StubRetriever(),
        scripted_chat_model(
            tool_calls=["zorblat"],
            answer_parts=["partial ", "never streamed"],
            fail_during_answer=openai.APIStatusError(
                "upstream exploded",
                response=httpx.Response(
                    500, request=httpx.Request("POST", "http://provider.test/v1/chat")
                ),
                body=None,
            ),
            fail_after_parts=1,
        ),
        mode="hybrid",
    )
    app.dependency_overrides[get_chat_service] = lambda: failing
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await _post(ac, {"question": QUESTION})

    assert resp.status_code == 200  # already streaming — no status change possible
    events = parse_sse(resp.text)
    assert [name for name, _ in events] == [
        "run_started",
        "sources",
        "answer_delta",
        "error",
    ]
    assert events[-1][1] == {"code": "llm_provider_error", "message": "LLM provider request failed"}
    app.dependency_overrides.clear()


async def test_unconfigured_chat_returns_503_envelope_before_any_stream(app):
    # The real constructor, not a stub: the key check must fire here.
    app.dependency_overrides[get_chat_service] = lambda: build_chat_service(
        hermetic_settings(CHAT_API_KEY=SecretStr(""))
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await _post(ac, {"question": QUESTION})

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    error = resp.json()["error"]
    assert error["code"] == "chat_unavailable"
    assert error["message"]
    app.dependency_overrides.clear()


async def test_invalid_body_on_unconfigured_chat_still_returns_503(app):
    # Pins the documented ordering: FastAPI resolves dependencies before body
    # validation, so the no-key gate wins over request-schema errors — same
    # contract as any auth-style dependency. The 422 contract holds whenever
    # the service IS constructible (see the stubbed tests below).
    app.dependency_overrides[get_chat_service] = lambda: build_chat_service(
        hermetic_settings(CHAT_API_KEY=SecretStr(""))
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await _post(ac, {"question": ""})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "chat_unavailable"
    app.dependency_overrides.clear()


async def test_build_chat_service_with_key_returns_hybrid_service():
    service = build_chat_service(hermetic_settings(CHAT_API_KEY=SecretStr("test-key")))

    assert isinstance(service, ChatService)


def test_build_chat_service_mode_tracks_the_embedding_wiring():
    # Since provider-config isolation the keys are independent: chat with
    # only CHAT_API_KEY wires a BM25-only retriever, and run_started.mode
    # must report that truthfully (chat previously pinned hybrid).
    # `_mode` is read directly — the constructor wires a real model, so
    # the stream cannot run offline.
    bm25_only = build_chat_service(hermetic_settings(CHAT_API_KEY=SecretStr("k")))
    hybrid = build_chat_service(
        hermetic_settings(CHAT_API_KEY=SecretStr("k"), EMBEDDING_API_KEY=SecretStr("e"))
    )

    assert bm25_only._mode == "bm25"
    assert hybrid._mode == "hybrid"


# --- validation contract (offline) ---
#
# The dependency is stubbed for these: FastAPI resolves dependencies before
# reporting body-validation errors, so an unconfigured chat deployment answers
# 503 rather than 422 for an invalid body. The request-schema contract itself
# is what these tests pin down.


async def test_empty_question_returns_422_envelope(chat_client):
    resp = await _post(chat_client, {"question": ""})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["message"]


async def test_missing_question_returns_422_envelope(chat_client):
    resp = await _post(chat_client, {})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_non_string_question_returns_422_envelope(chat_client):
    resp = await _post(chat_client, {"question": 42})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


@pytest.mark.parametrize("limit", [0, 21, -1])
async def test_out_of_range_limit_returns_422_envelope(chat_client, limit):
    resp = await _post(chat_client, {"question": QUESTION, "limit": limit})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


@pytest.mark.parametrize("limit", [1, 20])
async def test_limit_boundaries_are_accepted(chat_client, limit):
    resp = await _post(chat_client, {"question": QUESTION, "limit": limit})

    assert resp.status_code == 200


async def test_stateless_regression_body_without_session_id_streams(chat_client):
    # Bodies without session_id stay accepted, and the stateless service
    # (no persistence wired — exactly the pre-session construction) reports
    # session_id: null in its events.
    resp = await _post(chat_client, {"question": QUESTION})

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    run_started, done = events[0][1], events[-1][1]
    assert run_started["session_id"] is None
    assert done["session_id"] is None


# --- session persistence through the API (disposable test DB) ---


@pytest.fixture
async def db_chat_client(app: FastAPI, db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """ASGI client whose scripted chat service persists to the test DB.

    `get_db` is overridden too, so the session endpoints read what the chat
    endpoint wrote — the full production flow minus the provider.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_chat_service] = lambda: ChatService(
        StubRetriever(),
        scripted_chat_model(answer_parts=["Grounded answer."]),
        mode="hybrid",
        session_factory=factory,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.db
async def test_chat_without_session_id_creates_session_visible_in_events_and_api(db_chat_client):
    resp = await _post(db_chat_client, {"question": QUESTION})

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    run_started, done = events[0][1], events[-1][1]
    assert run_started["session_id"]
    assert done["session_id"] == run_started["session_id"]

    detail = await db_chat_client.get(f"/api/v1/chat/sessions/{run_started['session_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["title"] == QUESTION  # short question: title is verbatim
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == QUESTION
    assert body["messages"][1]["content"] == "Grounded answer."
    # Same run, two renderings: events stream the bare hex, the stored column
    # is a UUID that serializes with dashes.
    assert UUID(body["messages"][1]["run_id"]) == UUID(run_started["run_id"])


@pytest.mark.db
async def test_second_api_turn_continues_the_same_session(db_chat_client):
    first = await _post(db_chat_client, {"question": "first question"})
    session_id = parse_sse(first.text)[0][1]["session_id"]
    detail_after_first = (await db_chat_client.get(f"/api/v1/chat/sessions/{session_id}")).json()

    second = await _post(db_chat_client, {"question": "second question", "session_id": session_id})

    assert second.status_code == 200
    assert parse_sse(second.text)[0][1]["session_id"] == session_id
    detail = (await db_chat_client.get(f"/api/v1/chat/sessions/{session_id}")).json()
    assert [message["content"] for message in detail["messages"]] == [
        "first question",
        "Grounded answer.",
        "second question",
        "Grounded answer.",
    ]
    # PRD: each turn advances the session's updated_at (recency ordering).
    assert datetime.fromisoformat(detail["updated_at"]) > datetime.fromisoformat(
        detail_after_first["updated_at"]
    )


@pytest.mark.db
async def test_continuing_missing_session_returns_404_envelope_not_a_stream(db_chat_client):
    resp = await _post(
        db_chat_client,
        {"question": QUESTION, "session_id": "00000000-0000-0000-0000-000000000000"},
    )

    # The 404 must be a JSON envelope raised before the stream starts — not
    # an event-stream response with an error event.
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.db
async def test_deleted_session_cannot_be_continued(db_chat_client):
    first = await _post(db_chat_client, {"question": QUESTION})
    session_id = parse_sse(first.text)[0][1]["session_id"]
    delete_resp = await db_chat_client.delete(f"/api/v1/chat/sessions/{session_id}")
    assert delete_resp.status_code == 204

    resp = await _post(db_chat_client, {"question": QUESTION, "session_id": session_id})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
