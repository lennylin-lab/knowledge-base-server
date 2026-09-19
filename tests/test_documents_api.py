"""Documents API contract: status codes, envelopes, payload shapes."""

from __future__ import annotations

import hashlib

import pytest

pytestmark = pytest.mark.db

FM_DOC = "---\ntitle: Contract Note\ntags: [api, smoke]\n---\n\n# Body\n"
NO_FM_DOC = "Just some markdown, no front matter."
FM_DOC_HASH = hashlib.sha256(FM_DOC.encode("utf-8")).hexdigest()


async def test_create_document_returns_201_and_derived_columns(db_client):
    resp = await db_client.post("/api/v1/documents", json={"content": FM_DOC})

    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Contract Note"
    assert body["tags"] == ["api", "smoke"]
    assert body["index_status"] == "pending"
    assert "content" not in body


async def test_create_document_without_front_matter_falls_back_to_request_title(db_client):
    resp = await db_client.post(
        "/api/v1/documents", json={"content": NO_FM_DOC, "title": "Given Title"}
    )

    assert resp.status_code == 201
    assert resp.json()["title"] == "Given Title"


async def test_create_document_without_title_resolves_untitled(db_client):
    resp = await db_client.post("/api/v1/documents", json={"content": NO_FM_DOC})

    assert resp.status_code == 201
    assert resp.json()["title"] == "Untitled"


async def test_create_document_with_empty_content_returns_422_envelope(db_client):
    resp = await db_client.post("/api/v1/documents", json={"content": ""})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["message"]


async def test_create_document_with_invalid_front_matter_returns_422_envelope(db_client):
    resp = await db_client.post(
        "/api/v1/documents", json={"content": "---\ntags: [unbalanced\n---\nbody"}
    )

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_failed"
    assert "front matter" in error["message"]


async def test_get_document_returns_200_with_content(db_client):
    created = (await db_client.post("/api/v1/documents", json={"content": FM_DOC})).json()

    resp = await db_client.get(f"/api/v1/documents/{created['id']}")

    assert resp.status_code == 200
    assert resp.json()["content"] == FM_DOC
    assert resp.json()["title"] == "Contract Note"
    assert resp.json()["content_hash"] == FM_DOC_HASH


async def test_get_document_list_excludes_content_hash(db_client):
    await db_client.post("/api/v1/documents", json={"content": FM_DOC})

    resp = await db_client.get("/api/v1/documents")

    assert resp.status_code == 200
    assert "content_hash" not in resp.json()["items"][0]


async def test_patch_document_with_matching_expected_hash_returns_200(db_client):
    created = (await db_client.post("/api/v1/documents", json={"content": FM_DOC})).json()

    resp = await db_client.patch(
        f"/api/v1/documents/{created['id']}",
        json={"content": "new body", "expected_content_hash": FM_DOC_HASH},
    )

    assert resp.status_code == 200
    assert resp.json()["index_status"] == "pending"


async def test_patch_document_with_stale_expected_hash_returns_409_and_keeps_document(db_client):
    created = (await db_client.post("/api/v1/documents", json={"content": FM_DOC})).json()
    stale_hash = hashlib.sha256(b"stale").hexdigest()

    resp = await db_client.patch(
        f"/api/v1/documents/{created['id']}",
        json={"content": "new body", "expected_content_hash": stale_hash},
    )

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"]["expected_content_hash"] == stale_hash
    assert error["details"]["actual_content_hash"] == FM_DOC_HASH

    after = (await db_client.get(f"/api/v1/documents/{created['id']}")).json()
    assert after["content"] == FM_DOC
    assert after["title"] == "Contract Note"
    assert after["index_status"] == "pending"  # untouched: still the create-time status


async def test_get_missing_document_returns_404_envelope(db_client):
    resp = await db_client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"]


async def test_list_documents_excludes_content(db_client):
    await db_client.post("/api/v1/documents", json={"content": FM_DOC})

    resp = await db_client.get("/api/v1/documents")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert "content" not in items[0]


async def test_list_documents_filters_by_tag(db_client):
    await db_client.post("/api/v1/documents", json={"content": FM_DOC})
    await db_client.post("/api/v1/documents", json={"content": NO_FM_DOC})

    resp = await db_client.get("/api/v1/documents", params={"tag": "api"})

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Contract Note"


async def test_list_documents_filters_by_multiple_tags(db_client):
    await db_client.post("/api/v1/documents", json={"content": FM_DOC})
    await db_client.post("/api/v1/documents", json={"content": NO_FM_DOC})

    both = await db_client.get("/api/v1/documents", params={"tag": ["api", "smoke"]})
    missing = await db_client.get("/api/v1/documents", params={"tag": ["api", "other"]})

    assert both.status_code == 200
    assert [item["title"] for item in both.json()["items"]] == ["Contract Note"]
    assert missing.status_code == 200
    assert missing.json()["items"] == []


async def test_list_documents_rejects_invalid_cursor_with_422(db_client):
    resp = await db_client.get("/api/v1/documents", params={"cursor": "garbage!"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_list_documents_paginates_disjoint_pages_in_order(db_client):
    created = [
        (await db_client.post("/api/v1/documents", json={"content": f"doc {i}"})).json()
        for i in range(5)
    ]

    first = await db_client.get("/api/v1/documents", params={"limit": 2})
    assert first.status_code == 200
    first_page = first.json()
    assert [item["id"] for item in first_page["items"]] == [
        created[4]["id"],
        created[3]["id"],
    ]
    assert first_page["next_cursor"] is not None

    second = await db_client.get(
        "/api/v1/documents", params={"limit": 2, "cursor": first_page["next_cursor"]}
    )
    second_page = second.json()
    assert [item["id"] for item in second_page["items"]] == [
        created[2]["id"],
        created[1]["id"],
    ]
    first_ids = {item["id"] for item in first_page["items"]}
    second_ids = {item["id"] for item in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)


async def test_patch_document_returns_200_and_reindexes(db_client):
    created = (await db_client.post("/api/v1/documents", json={"content": FM_DOC})).json()

    resp = await db_client.patch(
        f"/api/v1/documents/{created['id']}",
        json={"content": "---\ntitle: Patched\ntags: [new]\n---\nbody"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Patched"
    assert body["tags"] == ["new"]
    assert body["index_status"] == "pending"


async def test_patch_document_with_empty_body_returns_422(db_client):
    created = (await db_client.post("/api/v1/documents", json={"content": FM_DOC})).json()

    resp = await db_client.patch(f"/api/v1/documents/{created['id']}", json={})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


async def test_patch_missing_document_returns_404(db_client):
    resp = await db_client.patch(
        "/api/v1/documents/00000000-0000-0000-0000-000000000000", json={"title": "x"}
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_delete_document_returns_204_then_404(db_client):
    created = (await db_client.post("/api/v1/documents", json={"content": FM_DOC})).json()

    delete_resp = await db_client.delete(f"/api/v1/documents/{created['id']}")
    get_resp = await db_client.get(f"/api/v1/documents/{created['id']}")
    list_resp = await db_client.get("/api/v1/documents")

    assert delete_resp.status_code == 204
    assert get_resp.status_code == 404
    assert list_resp.json()["items"] == []


async def test_delete_missing_document_returns_404(db_client):
    resp = await db_client.delete("/api/v1/documents/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
