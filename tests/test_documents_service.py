"""Document service rules: front matter, normalization, pagination, soft delete."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.document import Document, IndexStatus
from app.schemas.document import DocumentCreate, DocumentUpdate
from app.services.document import DocumentService, ReindexEnqueuer

pytestmark = pytest.mark.db


def make_service(session: AsyncSession) -> DocumentService:
    return DocumentService(session)


async def create_raw(
    session: AsyncSession, *, title: str, tags: list[str], created_at: datetime
) -> Document:
    """Insert a document bypassing the service (explicit timestamps for ties)."""
    document = Document(
        title=title, content="raw", tags=tags, created_at=created_at, updated_at=created_at
    )
    session.add(document)
    await session.flush()
    return document


async def mark_done(session: AsyncSession, doc_id: UUID) -> None:
    """Simulate the indexing pipeline having finished a document."""
    document = await session.get(Document, doc_id)
    assert document is not None
    document.index_status = IndexStatus.DONE
    await session.flush()


async def test_create_extracts_title_and_tags_from_front_matter(db_session):
    service = make_service(db_session)
    content = "---\ntitle: FM Title\ntags: [kotlin, fp]\n---\nbody"

    result = await service.create_document(DocumentCreate(content=content))

    assert result.title == "FM Title"
    assert result.tags == ["kotlin", "fp"]
    assert result.index_status == IndexStatus.PENDING


async def test_create_title_falls_back_to_request_then_untitled(db_session):
    service = make_service(db_session)

    from_request = await service.create_document(
        DocumentCreate(content="no fm here", title="Request Title")
    )
    from_default = await service.create_document(DocumentCreate(content="no fm here"))

    assert from_request.title == "Request Title"
    assert from_default.title == "Untitled"


@pytest.mark.parametrize(
    ("tags_yaml", "expected"),
    [
        # Quoted YAML strings keep surrounding whitespace; plain scalars strip it.
        ('tags: [" Kotlin ", "kotlin", "FP", "fp", "  "]', ["kotlin", "fp"]),
        ("tags: []", []),
    ],
)
async def test_create_normalizes_tags(db_session, tags_yaml, expected):
    service = make_service(db_session)

    result = await service.create_document(DocumentCreate(content=f"---\n{tags_yaml}\n---\nbody"))

    assert result.tags == expected


@pytest.mark.parametrize(
    "tags_yaml",
    [
        "tags: notalist",  # not a list
        "tags:\n  - 1\n  - two",  # non-str item
    ],
)
async def test_create_invalid_tags_raises_validation_error(db_session, tags_yaml):
    service = make_service(db_session)

    with pytest.raises(ValidationError) as exc_info:
        await service.create_document(DocumentCreate(content=f"---\n{tags_yaml}\n---\nbody"))

    assert exc_info.value.details["field"] == "tags"


async def test_create_malformed_yaml_raises_validation_error(db_session):
    service = make_service(db_session)

    with pytest.raises(ValidationError) as exc_info:
        await service.create_document(DocumentCreate(content="---\n a: b\n  c: d\ne: f\n---\n"))

    assert "front matter" in exc_info.value.message


async def test_get_missing_document_raises_not_found(db_session):
    service = make_service(db_session)

    with pytest.raises(NotFoundError):
        await service.get_document(uuid4())


async def test_list_filters_by_normalized_tag(db_session):
    service = make_service(db_session)
    await service.create_document(DocumentCreate(content="---\ntags: [Python]\n---\n"))
    await service.create_document(DocumentCreate(content="untagged"))

    exact = await service.list_documents(tags=["python"])
    upper = await service.list_documents(tags=[" PYTHON "])

    assert [item.title for item in exact.items] == ["Untitled"]
    assert [item.title for item in upper.items] == ["Untitled"]


async def test_list_requires_every_requested_tag(db_session):
    service = make_service(db_session)
    await service.create_document(
        DocumentCreate(content="---\ntitle: Both\ntags: [Python, Async]\n---\n")
    )
    await service.create_document(DocumentCreate(content="---\ntitle: Solo\ntags: [python]\n---\n"))
    await service.create_document(DocumentCreate(content="untagged"))

    both = await service.list_documents(tags=["PYTHON", "async"])
    deduped = await service.list_documents(tags=["PYTHON", " Async ", "python"])
    unfiltered = await service.list_documents(tags=[])

    assert [item.title for item in both.items] == ["Both"]
    assert [item.title for item in deduped.items] == ["Both"]
    assert len(unfiltered.items) == 3


async def test_list_paginates_disjoint_pages_newest_first(db_session):
    service = make_service(db_session)
    created = [await service.create_document(DocumentCreate(content=f"doc {i}")) for i in range(5)]
    created_ids = [doc.id for doc in created]

    first = await service.list_documents(limit=2)
    second = await service.list_documents(limit=2, cursor=first.next_cursor)
    third = await service.list_documents(limit=2, cursor=second.next_cursor)

    assert [item.id for item in first.items] == list(reversed(created_ids[-2:]))
    assert [item.id for item in second.items] == list(reversed(created_ids[1:3]))
    assert [item.id for item in third.items] == [created_ids[0]]
    assert third.next_cursor is None
    assert len({item.id for p in (first, second, third) for item in p.items}) == 5


async def test_pagination_survives_identical_created_at_via_id_tiebreak(db_session):
    same_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    await create_raw(db_session, title="a", tags=[], created_at=same_ts)
    await create_raw(db_session, title="b", tags=[], created_at=same_ts)
    service = make_service(db_session)

    first = await service.list_documents(limit=1)
    second = await service.list_documents(limit=1, cursor=first.next_cursor)

    assert len(first.items) == 1
    assert len(second.items) == 1
    assert first.items[0].id != second.items[0].id
    assert second.next_cursor is None


async def test_invalid_cursor_raises_validation_error(db_session):
    service = make_service(db_session)

    # Valid base64 ("aGVsbG8=" == "hello"), but not a cursor payload.
    with pytest.raises(ValidationError):
        await service.list_documents(cursor="aGVsbG8=")


async def test_update_rederives_title_and_tags_from_new_content(db_session):
    service = make_service(db_session)
    created = await service.create_document(
        DocumentCreate(content="---\ntitle: Old\ntags: [old]\n---\n")
    )

    updated = await service.update_document(
        created.id, DocumentUpdate(content="---\ntitle: New\ntags: [fresh]\n---\n")
    )

    assert updated.title == "New"
    assert updated.tags == ["fresh"]


async def test_update_title_only_keeps_tags(db_session):
    service = make_service(db_session)
    created = await service.create_document(
        DocumentCreate(content="---\ntitle: Old\ntags: [keep]\n---\n")
    )

    updated = await service.update_document(created.id, DocumentUpdate(title="Renamed"))

    assert updated.title == "Renamed"
    assert updated.tags == ["keep"]


async def test_update_resets_index_status_to_pending(db_session):
    service = make_service(db_session)
    created = await service.create_document(DocumentCreate(content="body"))
    # Simulate the future pipeline having finished this document.
    await mark_done(db_session, created.id)

    updated = await service.update_document(created.id, DocumentUpdate(title="touch"))

    assert updated.index_status == IndexStatus.PENDING


async def test_update_missing_document_raises_not_found(db_session):
    service = make_service(db_session)

    with pytest.raises(NotFoundError):
        await service.update_document(uuid4(), DocumentUpdate(title="x"))


async def test_soft_deleted_document_is_invisible(db_session):
    service = make_service(db_session)
    created = await service.create_document(DocumentCreate(content="---\ntitle: X\n---\n"))

    await service.delete_document(created.id)

    with pytest.raises(NotFoundError):
        await service.get_document(created.id)
    listing = await service.list_documents()
    assert listing.items == []
    with pytest.raises(NotFoundError):
        await service.delete_document(created.id)


async def test_delete_missing_document_raises_not_found(db_session):
    service = make_service(db_session)

    with pytest.raises(NotFoundError):
        await service.delete_document(uuid4())


# --- indexing enqueue wiring (services stay framework-free) ---


def capture_enqueues(captured: list[tuple[UUID, datetime]]) -> ReindexEnqueuer:
    """An enqueuer recording (doc_id, version) pairs — the ReindexEnqueuer shape."""
    return lambda doc_id, updated_at: captured.append((doc_id, updated_at))


async def test_create_enqueues_id_and_version_once_after_commit(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))

    created = await service.create_document(DocumentCreate(content="body"))

    # The version stamp is the committed write's updated_at (the value the
    # pipeline's generation guard compares against).
    assert captured == [(created.id, created.updated_at)]


async def test_update_enqueues_id_and_version_once_after_commit(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    created = await service.create_document(DocumentCreate(content="body"))
    captured.clear()

    updated = await service.update_document(created.id, DocumentUpdate(title="touch"))

    assert captured == [(created.id, updated.updated_at)]
    assert captured[0][1] != created.updated_at  # a new write bumps the version


async def test_failed_write_does_not_enqueue(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))

    with pytest.raises(ValidationError):
        await service.create_document(DocumentCreate(content="---\ntags: notalist\n---\n"))
    with pytest.raises(NotFoundError):
        await service.update_document(uuid4(), DocumentUpdate(title="x"))

    assert captured == []  # enqueue happens only after a successful commit


async def test_omitted_enqueuer_is_a_noop(db_session):
    service = DocumentService(db_session)  # default: no background indexing

    created = await service.create_document(DocumentCreate(content="body"))
    updated = await service.update_document(created.id, DocumentUpdate(title="touch"))

    assert updated.index_status == IndexStatus.PENDING


async def test_delete_does_not_enqueue(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    created = await service.create_document(DocumentCreate(content="body"))
    captured.clear()

    await service.delete_document(created.id)

    assert captured == []


# --- content hash guard (skip duplicate reindexing) ---


async def test_create_stores_sha256_content_hash(db_session):
    service = make_service(db_session)
    content = "---\ntitle: Hashed\n---\nbody"

    created = await service.create_document(DocumentCreate(content=content))

    document = await db_session.get(Document, created.id)
    assert document is not None
    assert document.content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()


async def test_update_identical_content_on_done_document_skips_reindex(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    content = "---\ntitle: Same\n---\nbody"
    created = await service.create_document(DocumentCreate(content=content))
    await mark_done(db_session, created.id)
    captured.clear()

    updated = await service.update_document(created.id, DocumentUpdate(content=content))

    assert updated.index_status == IndexStatus.DONE
    assert captured == []


async def test_update_identical_content_on_failed_document_retries(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    content = "retry body"
    created = await service.create_document(DocumentCreate(content=content))
    document = await db_session.get(Document, created.id)
    assert document is not None
    document.index_status = IndexStatus.FAILED
    await db_session.flush()
    captured.clear()

    updated = await service.update_document(created.id, DocumentUpdate(content=content))

    assert updated.index_status == IndexStatus.PENDING
    assert captured == [(created.id, updated.updated_at)]


async def test_update_changed_content_stores_new_hash_and_enqueues(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    created = await service.create_document(DocumentCreate(content="old body"))
    await mark_done(db_session, created.id)
    captured.clear()
    new_content = "new body"

    updated = await service.update_document(created.id, DocumentUpdate(content=new_content))

    assert updated.index_status == IndexStatus.PENDING
    assert captured == [(created.id, updated.updated_at)]
    document = await db_session.get(Document, created.id)
    assert document is not None
    assert document.content_hash == hashlib.sha256(new_content.encode("utf-8")).hexdigest()


async def test_update_identical_content_with_new_title_reindexes(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    created = await service.create_document(DocumentCreate(content="plain body", title="Old"))
    await mark_done(db_session, created.id)
    captured.clear()

    updated = await service.update_document(
        created.id, DocumentUpdate(content="plain body", title="New")
    )

    assert updated.title == "New"
    assert updated.index_status == IndexStatus.PENDING
    assert captured == [(created.id, updated.updated_at)]


async def test_title_only_same_title_skips_done_document(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    created = await service.create_document(DocumentCreate(content="---\ntitle: Same\n---\n"))
    await mark_done(db_session, created.id)
    captured.clear()

    updated = await service.update_document(created.id, DocumentUpdate(title="Same"))

    assert updated.index_status == IndexStatus.DONE
    assert captured == []


async def test_title_only_new_title_reindexes(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    created = await service.create_document(DocumentCreate(content="---\ntitle: Old\n---\n"))
    await mark_done(db_session, created.id)
    captured.clear()

    updated = await service.update_document(created.id, DocumentUpdate(title="New"))

    assert updated.title == "New"
    assert updated.index_status == IndexStatus.PENDING
    assert captured == [(created.id, updated.updated_at)]


async def test_null_content_hash_is_treated_as_changed(db_session):
    captured: list[tuple[UUID, datetime]] = []
    service = DocumentService(db_session, enqueuer=capture_enqueues(captured))
    content = "pre-backfill body"
    created = await service.create_document(DocumentCreate(content=content))
    # Simulate a row predating the hash backfill: status done, hash unknown.
    document = await db_session.get(Document, created.id)
    assert document is not None
    document.index_status = IndexStatus.DONE
    document.content_hash = None
    await db_session.flush()
    captured.clear()

    updated = await service.update_document(created.id, DocumentUpdate(content=content))

    assert updated.index_status == IndexStatus.PENDING
    assert captured == [(created.id, updated.updated_at)]
    document = await db_session.get(Document, created.id)
    assert document is not None
    assert document.content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()


async def test_skipped_update_does_not_bump_updated_at(db_session):
    service = make_service(db_session)
    content = "identical body"
    created = await service.create_document(DocumentCreate(content=content))
    # Force DONE via raw SQL: the ORM write path would bump updated_at
    # (onupdate), which is exactly the version this test guards. Expire so
    # the service re-reads the DONE status from the row.
    await db_session.execute(
        text("UPDATE documents SET index_status = 'done' WHERE id = :id"), {"id": created.id}
    )
    db_session.expire_all()

    updated = await service.update_document(created.id, DocumentUpdate(content=content))

    assert updated.updated_at == created.updated_at
