"""Agent-operation API contract: explicit drafts, stale-apply rejection,
idempotent apply/resume, the chat-history firewall, and the SSE draft stream.

Every test asserts the PRD review gates directly: no partial draft in
`chat_messages`, a stale apply publishes nothing, a duplicate apply creates
no second revision. The streaming draft tests follow the associations/summary
API pattern: the dependency is overridden with a FunctionModel-backed service
over the test database, so no request reaches a real provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openai import APIStatusError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import SessionDep, get_agent_operation_service
from app.models.chat import ChatMessage
from app.models.operation import AgentOperation, DocumentRevision, OperationState
from app.models.tenant import DEFAULT_TENANT_ID
from app.rag.retriever import SearchOutcome
from app.services.operation import AgentOperationService
from app.utils.ids import uuid7
from fakes import StubRetriever, parse_sse, scripted_draft_model

pytestmark = pytest.mark.db

FM_DOC = "---\ntitle: Base Note\ntags: [api]\n---\n\nOriginal body.\n"
DRAFT_CONTENT = "---\ntitle: Drafted Note\ntags: [api, drafted]\n---\n\nDrafted body.\n"
DRAFT_TITLE = "Generated Draft"


@pytest.fixture
def install_scripted_draft(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[[], list[str]]:
    """Swap the operation dependency for a scripted, offline draft service.

    Returns an installer whose result is the shared prompt recorder — what
    actually reached the model, in order (its length is the model-call count).
    """
    prompts: list[str] = []
    model_kwargs: dict[str, object] = {}

    def _override(session: SessionDep) -> AgentOperationService:
        return AgentOperationService(
            session,
            model=scripted_draft_model(
                DRAFT_CONTENT, DRAFT_TITLE, prompts=prompts, **model_kwargs
            ),
            retriever=StubRetriever(
                outcome=SearchOutcome(mode="bm25", items=[], es_hits=0, vector_hits=0)
            ),
        )

    def _install(**scripted: object) -> list[str]:
        nonlocal model_kwargs
        model_kwargs = scripted
        app.dependency_overrides[get_agent_operation_service] = _override
        return prompts

    return _install


async def _seed_document(db_client, content: str = FM_DOC) -> dict:
    """Create one live document and return its full read payload."""
    resp = await db_client.post("/api/v1/documents", json={"content": content})
    assert resp.status_code == 201
    return resp.json()


async def _create_operation(db_client, document: dict, **overrides) -> dict:
    payload = {
        "document_id": document["id"],
        "base_document_version": document["updated_at"],
        "draft": {"content": DRAFT_CONTENT},
        **overrides,
    }
    resp = await db_client.post("/api/v1/operations", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _revision_count(db_session: AsyncSession) -> int:
    return (
        await db_session.execute(select(func.count()).select_from(DocumentRevision))
    ).scalar_one()


async def test_create_operation_returns_completed_draft(db_client):
    document = await _seed_document(db_client)
    operation = await _create_operation(db_client, document)

    assert operation["state"] == "completed"
    assert operation["document_id"] == document["id"]
    assert operation["base_document_version"] == document["updated_at"]
    assert operation["draft"]["content"] == DRAFT_CONTENT
    assert operation["result"] is None


async def test_create_operation_idempotent_on_key(db_client):
    document = await _seed_document(db_client)
    first = await _create_operation(db_client, document, idempotency_key="retry-1")
    second = await _create_operation(db_client, document, idempotency_key="retry-1")

    assert first["id"] == second["id"]


async def test_get_operation_includes_draft(db_client):
    document = await _seed_document(db_client)
    operation = await _create_operation(db_client, document)

    resp = await db_client.get(f"/api/v1/operations/{operation['id']}")

    assert resp.status_code == 200
    assert resp.json()["draft"]["content"] == DRAFT_CONTENT


async def test_list_operations_scopes_to_document(db_client):
    document = await _seed_document(db_client)
    other = await _seed_document(db_client)
    await _create_operation(db_client, document)
    await _create_operation(db_client, other)

    resp = await db_client.get(f"/api/v1/operations/documents/{document['id']}")

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["document_id"] == document["id"]


async def test_get_missing_operation_returns_404_envelope(db_client):
    resp = await db_client.get(f"/api/v1/operations/{uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_apply_updates_document_creates_one_revision_marks_applied(db_client, db_session):
    document = await _seed_document(db_client)
    operation = await _create_operation(db_client, document)

    resp = await db_client.post(f"/api/v1/operations/{operation['id']}/apply", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"]["state"] == "applied"
    assert body["document"]["title"] == "Drafted Note"
    assert body["document"]["index_status"] == "pending"
    assert await _revision_count(db_session) == 1

    doc_resp = await db_client.get(f"/api/v1/documents/{document['id']}")
    assert doc_resp.json()["content"] == DRAFT_CONTENT


async def test_stale_apply_rejected_without_mutation(db_client, db_session):
    document = await _seed_document(db_client)
    operation = await _create_operation(db_client, document)
    # The document moves on after the draft: a real edit, not a no-op touch.
    patch = await db_client.patch(
        f"/api/v1/documents/{document['id']}",
        json={"content": "---\ntitle: Base Note\ntags: [api]\n---\n\nEdited elsewhere.\n"},
    )
    assert patch.status_code == 200

    resp = await db_client.post(f"/api/v1/operations/{operation['id']}/apply", json={})

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"]["current_version"]
    # Zero writes: the document keeps the concurrent edit, no revision exists,
    # and the operation is still an applicable completed draft.
    doc_resp = await db_client.get(f"/api/v1/documents/{document['id']}")
    assert doc_resp.json()["content"].endswith("Edited elsewhere.\n")
    assert await _revision_count(db_session) == 0
    op_resp = await db_client.get(f"/api/v1/operations/{operation['id']}")
    assert op_resp.json()["state"] == "completed"


async def test_duplicate_apply_is_idempotent_single_revision(db_client, db_session):
    document = await _seed_document(db_client)
    operation = await _create_operation(db_client, document)

    first = await db_client.post(f"/api/v1/operations/{operation['id']}/apply", json={})
    second = await db_client.post(f"/api/v1/operations/{operation['id']}/apply", json={})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["revision"] == second.json()["revision"]
    assert await _revision_count(db_session) == 1


async def test_apply_running_operation_conflicts(db_client, db_session):
    document = await _seed_document(db_client)
    db_session.add(
        AgentOperation(
            id=uuid7(),
            tenant_id=DEFAULT_TENANT_ID,
            document_id=UUID(document["id"]),
            base_document_version=None,
            state=OperationState.RUNNING,
        )
    )
    await db_session.commit()
    operation_id = (await db_session.execute(select(AgentOperation.id))).scalar_one()

    resp = await db_client.post(f"/api/v1/operations/{operation_id}/apply", json={})

    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["state"] == "running"
    assert await _revision_count(db_session) == 0


async def test_resume_interrupted_then_apply(db_client, db_session):
    document = await _seed_document(db_client)
    db_session.add(
        AgentOperation(
            id=uuid7(),
            tenant_id=DEFAULT_TENANT_ID,
            document_id=UUID(document["id"]),
            base_document_version=datetime.fromisoformat(document["updated_at"]),
            state=OperationState.INTERRUPTED,
            draft={"content": DRAFT_CONTENT, "title": None},
        )
    )
    await db_session.commit()
    operation_id = (await db_session.execute(select(AgentOperation.id))).scalar_one()

    resp = await db_client.post(f"/api/v1/operations/{operation_id}/resume", json={})
    assert resp.status_code == 200
    assert resp.json()["state"] == "completed"

    applied = await db_client.post(f"/api/v1/operations/{operation_id}/apply", json={})
    assert applied.status_code == 200
    assert applied.json()["operation"]["state"] == "applied"


async def test_resume_applied_operation_conflicts(db_client):
    document = await _seed_document(db_client)
    operation = await _create_operation(db_client, document)
    applied = await db_client.post(f"/api/v1/operations/{operation['id']}/apply", json={})
    assert applied.status_code == 200

    resp = await db_client.post(f"/api/v1/operations/{operation['id']}/resume", json={})

    assert resp.status_code == 409


async def test_operations_never_touch_chat_history(db_client, db_session):
    """The review gate: agent drafts/interrupted runs never enter chat."""
    document = await _seed_document(db_client)
    await _create_operation(db_client, document, idempotency_key="hist-1")
    db_session.add(
        AgentOperation(
            id=uuid7(),
            tenant_id=DEFAULT_TENANT_ID,
            document_id=UUID(document["id"]),
            base_document_version=None,
            state=OperationState.INTERRUPTED,
            draft={"content": DRAFT_CONTENT, "title": None},
        )
    )
    await db_session.commit()
    await db_client.post(f"/api/v1/operations/{uuid4()}/apply", json={})

    count = (await db_session.execute(select(func.count()).select_from(ChatMessage))).scalar_one()
    assert count == 0


# --- streaming draft (SSE) ---


async def test_draft_streams_event_order_and_persists_completed(
    db_client, db_session, install_scripted_draft
):
    """Canonical order run_started -> draft_delta* -> draft -> done; the
    operation lands `completed` with the structured draft, and chat history
    stays untouched."""
    document = await _seed_document(db_client)
    prompts = install_scripted_draft()

    resp = await db_client.post(f"/api/v1/operations/draft?document_id={document['id']}")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    names = [name for name, _ in events]
    assert names[:2] == ["run_started", "draft_delta"]
    assert names[-2:] == ["draft", "done"]
    assert names[2:-2] == ["draft_delta"] * (len(names) - 4)
    assert len(names) >= 5  # at least the fake's three argument fragments
    run_started = events[0][1]
    assert run_started["kind"] == "draft"
    assert run_started["document_id"] == document["id"]
    assert run_started["run_id"]
    deltas = [body for name, body in events if name == "draft_delta"]
    assert all(d["run_id"] == run_started["run_id"] for d in deltas)
    # Concatenation invariant: the raw fragments reassemble the tool-call
    # argument JSON the fake streamed, exactly, with no loss or duplication.
    assert "".join(d["delta"] for d in deltas) == json.dumps(
        {"content": DRAFT_CONTENT, "title": DRAFT_TITLE}
    )
    body = events[-2][1]
    assert set(body) == {"operation_id", "state", "content", "title"}
    assert body["state"] == "completed"
    assert body["content"] == DRAFT_CONTENT
    assert body["title"] == DRAFT_TITLE
    done = events[-1][1]
    assert done["run_id"] == run_started["run_id"]
    assert done["outcome"] == "success"
    assert done["latency_ms"] >= 0
    assert len(prompts) == 1  # exactly one model call

    # Terminal state persisted BEFORE the draft event: fully inspectable.
    op_resp = await db_client.get(f"/api/v1/operations/{body['operation_id']}")
    operation = op_resp.json()
    assert operation["state"] == "completed"
    assert operation["draft"]["content"] == DRAFT_CONTENT
    assert operation["error"] is None

    count = (await db_session.execute(select(func.count()).select_from(ChatMessage))).scalar_one()
    assert count == 0  # the chat-history firewall holds for streamed drafts too


async def test_draft_stream_skips_empty_opening_args_serialization(
    db_client, db_session, install_scripted_draft
):
    """A provider opening chunk carrying EMPTY arguments (observed live with
    gpt-5.5 through the gateway) materializes a part whose `{}` serialization
    is not a real fragment: it must never stream — the `draft_delta`
    concatenation stays exactly the final argument JSON."""
    document = await _seed_document(db_client)
    prompts = install_scripted_draft(open_args_json="")

    resp = await db_client.post(f"/api/v1/operations/draft?document_id={document['id']}")

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "run_started"
    assert names[-2:] == ["draft", "done"]
    deltas = [body for name, body in events if name == "draft_delta"]
    assert deltas, "the real argument fragments must still stream"
    # No phantom `{}` prepended; the concatenation is the args JSON exactly.
    assert "".join(d["delta"] for d in deltas) == json.dumps(
        {"content": DRAFT_CONTENT, "title": DRAFT_TITLE}
    )
    assert events[-2][1]["content"] == DRAFT_CONTENT
    assert len(prompts) == 1


async def test_draft_provider_failure_emits_single_error_leaves_failed_resumable(
    app, db_client, db_session, session_factory
):
    """Mid-stream provider failure: run_started stands, exactly one terminal
    `error`, and the operation is durably `failed` with error details — and
    still resumable."""
    document = await _seed_document(db_client)
    prompts: list[str] = []

    def _override(session: SessionDep) -> AgentOperationService:
        return AgentOperationService(
            session,
            model=scripted_draft_model(
                "never",
                fail=APIStatusError(
                    "upstream exploded",
                    response=httpx.Response(
                        500, request=httpx.Request("POST", "http://provider.test/v1/chat")
                    ),
                    body=None,
                ),
                prompts=prompts,
            ),
            retriever=StubRetriever(
                outcome=SearchOutcome(mode="bm25", items=[], es_hits=0, vector_hits=0)
            ),
        )

    app.dependency_overrides[get_agent_operation_service] = _override

    resp = await db_client.post(f"/api/v1/operations/draft?document_id={document['id']}")

    # Streaming had already begun: run_started stands, one terminal error, no done.
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert [name for name, _ in events] == ["run_started", "error"]
    assert events[-1][1]["code"] == "llm_provider_error"
    assert "upstream exploded" not in events[-1][1]["message"]  # no internals leak

    row = (
        await db_session.execute(select(AgentOperation).order_by(AgentOperation.created_at))
    ).scalar_one()
    assert row.state is OperationState.FAILED
    assert row.error == {"error_class": "APIStatusError"}

    resumed = await db_client.post(f"/api/v1/operations/{row.id}/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "completed"
    app.dependency_overrides.clear()


async def test_draft_failure_after_deltas_emits_single_error_leaves_failed_resumable(
    app, db_client, db_session, session_factory
):
    """Mid-stream failure AFTER deltas were sent: run_started and the already-
    forwarded `draft_delta` fragments stand, exactly one terminal `error` (no
    done), and the operation is durably `failed` and resumable — the client
    discards the partial deltas."""
    document = await _seed_document(db_client)
    prompts: list[str] = []
    failure = APIStatusError(
        "upstream exploded",
        response=httpx.Response(
            500, request=httpx.Request("POST", "http://provider.test/v1/chat")
        ),
        body=None,
    )

    def _override(session: SessionDep) -> AgentOperationService:
        return AgentOperationService(
            session,
            model=scripted_draft_model(
                "never",
                # name chunk + first args fragment streamed, then the provider dies
                fail_after_fragments=failure,
                fragments_before_fail=3,
                prompts=prompts,
            ),
            retriever=StubRetriever(
                outcome=SearchOutcome(mode="bm25", items=[], es_hits=0, vector_hits=0)
            ),
        )

    app.dependency_overrides[get_agent_operation_service] = _override

    resp = await db_client.post(f"/api/v1/operations/draft?document_id={document['id']}")

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    names = [name for name, _ in events]
    assert names == ["run_started", "draft_delta", "error"]
    assert events[1][1]["delta"]  # the first fragment got through verbatim
    assert events[-1][1]["code"] == "llm_provider_error"

    row = (
        await db_session.execute(select(AgentOperation).order_by(AgentOperation.created_at))
    ).scalar_one()
    assert row.state is OperationState.FAILED
    assert row.draft is None  # nothing partial was ever persisted

    resumed = await db_client.post(f"/api/v1/operations/{row.id}/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "completed"
    app.dependency_overrides.clear()


async def test_draft_of_missing_document_returns_404_envelope(db_client, install_scripted_draft):
    """Pre-stream failure: the priming pull keeps the 404 a JSON envelope,
    never a 200 stream."""
    missing = uuid4()
    install_scripted_draft()

    resp = await db_client.post(f"/api/v1/operations/draft?document_id={missing}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_unconfigured_draft_returns_503_envelope(app):
    """No LLM wiring in the service (no model/retriever): the draft gate
    fires pre-stream as chat's one no-LLM-fallback 503 code."""
    document_id = uuid4()

    async def _override(session: SessionDep) -> AgentOperationService:
        return AgentOperationService(session)  # no model, no retriever

    app.dependency_overrides[get_agent_operation_service] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(f"/api/v1/operations/draft?document_id={document_id}")

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    error = resp.json()["error"]
    assert error["code"] == "chat_unavailable"
    assert error["message"]
    app.dependency_overrides.clear()
