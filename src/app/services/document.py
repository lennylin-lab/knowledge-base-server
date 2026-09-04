"""Document business logic: front matter, transactions, domain errors."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
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
from app.utils.cursor import decode_id_cursor, encode_id_cursor

logger = structlog.get_logger(__name__)

# The service stays framework-free: how indexing gets scheduled (FastAPI
# BackgroundTasks, a queue, ...) is the injecting caller's concern. The
# enqueued `updated_at` is the document version observed at commit time —
# the indexing pipeline's generation guard uses it to skip jobs that a
# newer save has already superseded (see `rag/indexer.py`).
ReindexEnqueuer = Callable[[UUID, datetime], None]


def _normalize_tags(raw_tags: Iterable[str]) -> list[str]:
    """Trim, lowercase, drop empties, and de-duplicate (order preserved).

    Shared by stored tags (front matter) and query filters so both sides of
    a tag comparison are normalized identically.
    """
    tags: list[str] = []
    for raw_tag in raw_tags:
        normalized = raw_tag.strip().lower()
        if normalized and normalized not in tags:
            tags.append(normalized)
    return tags


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
    return resolved_title, _normalize_tags(raw_tags)


class DocumentService:
    """Orchestrates front-matter parsing, repository calls, and commits."""

    def __init__(self, session: AsyncSession, enqueuer: ReindexEnqueuer | None = None) -> None:
        self._session = session
        self._repo = DocumentRepository(session)
        self._enqueuer = enqueuer

    def _enqueue_indexing(self, doc_id: UUID, updated_at: datetime) -> None:
        """Schedule re-indexing; `None` enqueuer (default) is a no-op.

        `updated_at` is the version stamp of the just-committed write (loaded
        back from the server by the repository's post-flush refresh) — the
        pipeline's stale-job guard compares against it.
        """
        if self._enqueuer is not None:
            self._enqueuer(doc_id, updated_at)

    async def create_document(self, payload: DocumentCreate) -> DocumentRead:
        """Persist a new document derived from its front matter."""
        title, tags = _parse_front_matter(payload.content, payload.title)
        document = Document(title=title, content=payload.content, tags=tags)
        document = await self._repo.create(document)
        await self._session.commit()
        logger.info("document_created", document_id=str(document.id), title=document.title)
        # After commit only — a rolled-back write must never be indexed.
        self._enqueue_indexing(document.id, document.updated_at)
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
        tags: Sequence[str] | None = None,
    ) -> DocumentPage:
        """Keyset-paginated listing, optionally filtered by tag membership.

        Multiple tags AND together: a document is listed only when it carries
        every requested tag. Requested tags go through the same normalization
        as stored tags (trim, lowercase, dedupe); an empty/absent filter
        lists everything.
        """
        decoded = decode_id_cursor(cursor) if cursor is not None else None
        normalized_tags = _normalize_tags(tags) if tags else None
        rows = await self._repo.list_page(cursor=decoded, limit=limit, tags=normalized_tags)

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_id_cursor(last.id)

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
        self._enqueue_indexing(document.id, document.updated_at)
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
