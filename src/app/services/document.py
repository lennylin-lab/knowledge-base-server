"""Document business logic: front matter, transactions, domain errors."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

import frontmatter
import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.document import Document, IndexStatus
from app.repositories.document import DocumentRepository
from app.schemas.document import (
    DocumentCreate,
    DocumentPage,
    DocumentRead,
    DocumentReadDetail,
    DocumentUpdate,
)

logger = structlog.get_logger(__name__)

# Cursor payload keys stay short — cursors travel in every list request.
_CURSOR_CREATED_AT_KEY = "ca"
_CURSOR_ID_KEY = "id"

# The service stays framework-free: how indexing gets scheduled (FastAPI
# BackgroundTasks, a queue, ...) is the injecting caller's concern.
ReindexEnqueuer = Callable[[UUID], None]


def _parse_front_matter(content: str, request_title: str | None) -> tuple[str, list[str]]:
    """Derive (title, tags) from front matter in `content`.

    Title resolution: front-matter title (non-empty str) → request title →
    "Untitled". Tags come only from front matter; anything but an absent
    field or a list of strings is a validation failure.
    """
    try:
        metadata, _ = frontmatter.parse(content)
    except yaml.YAMLError as exc:
        # Message from the parser; no traceback internals leak.
        raise ValidationError(f"Invalid front matter: {exc}") from exc

    fm_title = metadata.get("title")
    title = fm_title if isinstance(fm_title, str) and fm_title.strip() else None
    resolved_title = (title or request_title or "").strip() or "Untitled"

    raw_tags = metadata.get("tags", [])
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        raise ValidationError(
            "Invalid front matter: 'tags' must be a list of strings",
            details={"field": "tags"},
        )
    tags: list[str] = []
    for raw_tag in raw_tags:
        normalized = raw_tag.strip().lower()
        if normalized and normalized not in tags:
            tags.append(normalized)

    return resolved_title, tags


def _encode_cursor(created_at: datetime, doc_id: UUID) -> str:
    """Opaque keyset cursor: urlsafe-base64 JSON of (created_at, id)."""
    payload = json.dumps(
        {_CURSOR_CREATED_AT_KEY: created_at.isoformat(), _CURSOR_ID_KEY: str(doc_id)}
    )
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Inverse of `_encode_cursor`; any malformed input is a 422."""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(data[_CURSOR_CREATED_AT_KEY]), UUID(data[_CURSOR_ID_KEY])
    except (ValueError, KeyError, TypeError) as exc:
        # binascii.Error (bad base64) and JSONDecodeError subclass ValueError.
        raise ValidationError("Invalid cursor", details={"cursor": cursor}) from exc


class DocumentService:
    """Orchestrates front-matter parsing, repository calls, and commits."""

    def __init__(self, session: AsyncSession, enqueuer: ReindexEnqueuer | None = None) -> None:
        self._session = session
        self._repo = DocumentRepository(session)
        self._enqueuer = enqueuer

    def _enqueue_indexing(self, doc_id: UUID) -> None:
        """Schedule re-indexing; `None` enqueuer (default) is a no-op."""
        if self._enqueuer is not None:
            self._enqueuer(doc_id)

    async def create_document(self, payload: DocumentCreate) -> DocumentRead:
        """Persist a new document derived from its front matter."""
        title, tags = _parse_front_matter(payload.content, payload.title)
        document = Document(title=title, content=payload.content, tags=tags)
        document = await self._repo.create(document)
        await self._session.commit()
        logger.info("document_created", document_id=str(document.id), title=document.title)
        # After commit only — a rolled-back write must never be indexed.
        self._enqueue_indexing(document.id)
        return DocumentRead.model_validate(document)

    async def get_document(self, doc_id: UUID) -> DocumentReadDetail:
        """Return one document (with content) or raise NotFoundError."""
        document = await self._get_or_raise(doc_id)
        return DocumentReadDetail.model_validate(document)

    async def list_documents(
        self,
        *,
        cursor: str | None = None,
        limit: int = 20,
        tag: str | None = None,
    ) -> DocumentPage:
        """Keyset-paginated listing, optionally filtered by tag membership."""
        decoded = _decode_cursor(cursor) if cursor is not None else None
        normalized_tag = tag.strip().lower() if tag else None
        rows = await self._repo.list_page(cursor=decoded, limit=limit, tag=normalized_tag)

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)

        return DocumentPage(
            items=[DocumentRead.model_validate(row) for row in rows],
            next_cursor=next_cursor,
        )

    async def update_document(self, doc_id: UUID, payload: DocumentUpdate) -> DocumentRead:
        """Apply a partial update; content changes re-derive title and tags.

        Any write resets `index_status` to PENDING — a changed document needs
        re-indexing (title/tags feed the search index too, not just content).
        """
        document = await self._get_or_raise(doc_id)
        if payload.content is not None:
            title, tags = _parse_front_matter(payload.content, payload.title)
            document.content = payload.content
            document.title = title
            document.tags = tags
        elif payload.title is not None:
            document.title = payload.title.strip() or "Untitled"

        document.index_status = IndexStatus.PENDING
        document = await self._repo.update(document)
        await self._session.commit()
        logger.info(
            "document_updated",
            document_id=str(document.id),
            title=document.title,
        )
        self._enqueue_indexing(document.id)
        return DocumentRead.model_validate(document)

    async def delete_document(self, doc_id: UUID) -> None:
        """Soft-delete a document; it disappears from every read path."""
        document = await self._get_or_raise(doc_id)
        await self._repo.soft_delete(document)
        await self._session.commit()
        logger.info("document_deleted", document_id=str(document.id))

    async def _get_or_raise(self, doc_id: UUID) -> Document:
        document = await self._repo.get_by_id(doc_id)
        if document is None:
            raise NotFoundError(f"Document {doc_id} not found")
        return document
