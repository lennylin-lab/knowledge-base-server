"""Agent-operation API contract: explicit drafts, stale-apply rejection,
idempotent apply/resume, and the chat-history firewall.

Every test asserts the PRD review gates directly: no partial draft in
`chat_messages`, a stale apply publishes nothing, a duplicate apply creates
no second revision.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage
from app.models.operation import AgentOperation, DocumentRevision, OperationState
from app.utils.ids import uuid7

pytestmark = pytest.mark.db

FM_DOC = "---\ntitle: Base Note\ntags: [api]\n---\n\nOriginal body.\n"
DRAFT_CONTENT = "---\ntitle: Drafted Note\ntags: [api, drafted]\n---\n\nDrafted body.\n"


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
