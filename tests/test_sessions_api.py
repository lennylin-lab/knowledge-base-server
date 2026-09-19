"""Chat sessions API contract: pagination ordering, chronological detail,
soft-delete invisibility.

Sessions are seeded directly through the repositories (the chat endpoint's
implicit creation has its own coverage in test_chat_api.py); explicit
timestamps make the updated_at/created_at orderings deterministic — server
defaults would tie inside one transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.repositories.chat import ChatMessageRepository, ChatSessionRepository

pytestmark = pytest.mark.db

_BASE = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
MISSING_ID = "00000000-0000-0000-0000-000000000000"
TENANT_B_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")


async def _seed_session(
    db_session: AsyncSession,
    *,
    title: str = "Session",
    updated_at: datetime | None = None,
    messages: list[tuple[MessageRole, str]] = (),
) -> ChatSession:
    """Insert one session (optionally with messages) and commit."""
    chat_session = await ChatSessionRepository(db_session).create(
        ChatSession(tenant_id=DEFAULT_TENANT_ID, title=title, updated_at=updated_at)
    )
    message_repo = ChatMessageRepository(db_session)
    for index, (role, content) in enumerate(messages):
        await message_repo.add(
            ChatMessage(
                session_id=chat_session.id,
                role=role,
                content=content,
                created_at=_BASE + timedelta(seconds=index),
            )
        )
    await db_session.commit()
    return chat_session


async def test_list_sessions_orders_most_recently_updated_first(db_client, db_session):
    seeded = [
        await _seed_session(db_session, title=f"s{i}", updated_at=_BASE + timedelta(hours=i))
        for i in range(3)
    ]

    resp = await db_client.get("/api/v1/chat/sessions")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [str(s.id) for s in reversed(seeded)]
    assert resp.json()["next_cursor"] is None
    assert set(items[0]) == {"id", "title", "created_at", "updated_at"}


async def test_list_sessions_paginates_disjoint_pages_in_order(db_client, db_session):
    seeded = [
        await _seed_session(db_session, title=f"s{i}", updated_at=_BASE + timedelta(hours=i))
        for i in range(5)
    ]
    expected_all = [str(s.id) for s in reversed(seeded)]

    first = (await db_client.get("/api/v1/chat/sessions", params={"limit": 2})).json()
    assert [item["id"] for item in first["items"]] == expected_all[:2]
    assert first["next_cursor"] is not None

    second = (
        await db_client.get(
            "/api/v1/chat/sessions", params={"limit": 2, "cursor": first["next_cursor"]}
        )
    ).json()
    assert [item["id"] for item in second["items"]] == expected_all[2:4]

    third = (
        await db_client.get(
            "/api/v1/chat/sessions", params={"limit": 2, "cursor": second["next_cursor"]}
        )
    ).json()
    assert [item["id"] for item in third["items"]] == expected_all[4:]
    assert third["next_cursor"] is None

    pages = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert len(pages) == len(set(pages))  # disjoint cursor pages


async def test_list_sessions_rejects_invalid_cursor_with_422(db_client):
    resp = await db_client.get("/api/v1/chat/sessions", params={"cursor": "garbage!"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_list_sessions_rejects_out_of_range_limit(db_client, limit):
    resp = await db_client.get("/api/v1/chat/sessions", params={"limit": limit})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_get_session_returns_messages_chronologically(db_client, db_session):
    seeded = await _seed_session(
        db_session,
        title="Contract",
        messages=[(MessageRole.USER, "first"), (MessageRole.ASSISTANT, "second")],
    )

    resp = await db_client.get(f"/api/v1/chat/sessions/{seeded.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(seeded.id)
    assert body["title"] == "Contract"
    assert [message["content"] for message in body["messages"]] == ["first", "second"]
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert set(body["messages"][0]) == {"id", "role", "content", "run_id", "created_at"}


async def test_get_session_paginated_first_page_returns_newest_ascending(db_client, db_session):
    seeded = await _seed_session(
        db_session,
        messages=[(MessageRole.USER, f"m{i}") for i in range(5)],
    )

    resp = await db_client.get(f"/api/v1/chat/sessions/{seeded.id}", params={"limit": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is not None
    assert [m["content"] for m in body["items"]] == ["m3", "m4"]
    assert set(body) == {"items", "next_cursor"}


async def test_get_session_pagination_walk_reconstructs_full_history(db_client, db_session):
    seeded = await _seed_session(
        db_session,
        messages=[(MessageRole.USER, f"m{i}") for i in range(7)],
    )
    expected = [f"m{i}" for i in range(7)]

    pages: list[list[str]] = []
    cursor: str | None = None
    for _ in range(10):  # bounded walk; a non-terminating cursor would overrun
        params: dict[str, object] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        body = (await db_client.get(f"/api/v1/chat/sessions/{seeded.id}", params=params)).json()
        pages.append([m["content"] for m in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    collected = [c for page in reversed(pages) for c in page]  # oldest page last
    assert collected == expected  # full history, chronological, no gaps
    assert len(collected) == len(set(collected))  # no dupes
    assert cursor is None  # terminated on the oldest page


async def test_get_session_page_when_limit_equals_total_has_null_cursor(db_client, db_session):
    seeded = await _seed_session(db_session, messages=[(MessageRole.USER, "only")])

    resp = await db_client.get(f"/api/v1/chat/sessions/{seeded.id}", params={"limit": 5})

    assert resp.status_code == 200
    body = resp.json()
    assert [m["content"] for m in body["items"]] == ["only"]
    assert body["next_cursor"] is None


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_get_session_rejects_out_of_range_limit(db_client, db_session, limit):
    seeded = await _seed_session(db_session)

    resp = await db_client.get(f"/api/v1/chat/sessions/{seeded.id}", params={"limit": limit})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_get_session_rejects_invalid_cursor_with_422(db_client, db_session):
    seeded = await _seed_session(db_session)

    resp = await db_client.get(
        f"/api/v1/chat/sessions/{seeded.id}", params={"limit": 2, "cursor": "garbage!"}
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_get_session_paginated_isolated_cross_tenant(db_client, db_session):
    db_session.add(Tenant(id=TENANT_B_ID, slug="tenant-b", name="Tenant B"))
    await db_session.flush()
    foreign = await ChatSessionRepository(db_session).create(
        ChatSession(tenant_id=TENANT_B_ID, title="foreign", updated_at=_BASE)
    )
    await ChatMessageRepository(db_session).add(
        ChatMessage(session_id=foreign.id, role=MessageRole.USER, content="secret")
    )
    await db_session.commit()

    resp = await db_client.get(f"/api/v1/chat/sessions/{foreign.id}", params={"limit": 10})
    cursor_resp = await db_client.get(
        f"/api/v1/chat/sessions/{foreign.id}", params={"limit": 10, "cursor": "x"}
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert cursor_resp.status_code == 404


async def test_get_missing_session_returns_404_envelope(db_client):
    resp = await db_client.get(f"/api/v1/chat/sessions/{MISSING_ID}")

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"]


async def test_delete_session_returns_204_then_invisible_everywhere(db_client, db_session):
    seeded = await _seed_session(db_session, messages=[(MessageRole.USER, "q")])

    delete_resp = await db_client.delete(f"/api/v1/chat/sessions/{seeded.id}")
    get_resp = await db_client.get(f"/api/v1/chat/sessions/{seeded.id}")
    list_resp = await db_client.get("/api/v1/chat/sessions")

    assert delete_resp.status_code == 204
    assert get_resp.status_code == 404
    assert get_resp.json()["error"]["code"] == "not_found"
    assert list_resp.json()["items"] == []


async def test_delete_missing_session_returns_404(db_client):
    resp = await db_client.delete(f"/api/v1/chat/sessions/{MISSING_ID}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
