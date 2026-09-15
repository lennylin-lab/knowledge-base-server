"""Document business logic: front matter, transactions, domain errors."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from uuid import UUID

import frontmatter
import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import SEARCH_EPOCH_KEY, Cache
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
# enqueued `tenant_id` scopes the background run (the pipeline's document
# read is tenant-filtered); the enqueued `updated_at` is the document version
# observed at commit time — the indexing pipeline's generation guard uses it
# to skip jobs that a newer save has already superseded (see `rag/indexer.py`).
# Enqueues only ever follow a real change: update_document's content-hash
# guard skips byte-identical saves of already-indexed documents.
ReindexEnqueuer = Callable[[UUID, UUID, datetime], None]


def _content_hash(content: str) -> str:
    """SHA-256 hex digest of the raw content (front matter included)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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

    def __init__(
        self,
        session: AsyncSession,
        enqueuer: ReindexEnqueuer | None = None,
        cache: Cache | None = None,
    ) -> None:
        self._session = session
        self._repo = DocumentRepository(session)
        self._enqueuer = enqueuer
        # Optional best-effort cache handle: every committed write bumps the
        # global search epoch, invalidating the whole search-result cache
        # (coarse but always correct — see the cache task design). None =
        # no-op, the pre-cache behavior.
        self._cache = cache

    def _enqueue_indexing(self, doc_id: UUID, tenant_id: UUID, updated_at: datetime) -> None:
        """Schedule re-indexing; `None` enqueuer (default) is a no-op.

        `tenant_id` scopes the background run to this document's tenant;
        `updated_at` is the version stamp of the just-committed write (loaded
        back from the server by the repository's post-flush refresh) — the
        pipeline's stale-job guard compares against it.
        """
        if self._enqueuer is not None:
            self._enqueuer(doc_id, tenant_id, updated_at)

    async def _bump_search_epoch(self) -> None:
        """Bump the search-cache epoch AFTER commit — a rolled-back write must
        never bust the cache. The `Cache` contract never raises, but the bump
        stays best-effort regardless: a failure only costs one stale window,
        never a failed write."""
        if self._cache is None:
            return
        try:
            await self._cache.incr(SEARCH_EPOCH_KEY)
        except Exception as exc:
            logger.warning(
                "cache_error", domain="search", op="incr", error_class=type(exc).__name__
            )

    async def create_document(self, payload: DocumentCreate, *, tenant_id: UUID) -> DocumentRead:
        """Persist a new document derived from its front matter.

        `tenant_id` is the caller's resolved tenant scope — the owning tenant
        of the new row (required: there is no default-to-a-tenant fallback in
        the service layer).
        """
        title, tags = _parse_front_matter(payload.content, payload.title)
        document = Document(
            tenant_id=tenant_id,
            title=title,
            content=payload.content,
            tags=tags,
            content_hash=_content_hash(payload.content),
        )
        document = await self._repo.create(document)
        await self._session.commit()
        logger.info("document_created", document_id=str(document.id), title=document.title)
        # After commit only — a rolled-back write must never be indexed.
        self._enqueue_indexing(document.id, document.tenant_id, document.updated_at)
        await self._bump_search_epoch()
        return DocumentRead.model_validate(document)

    async def get_document(self, doc_id: UUID, *, tenant_id: UUID) -> DocumentReadDetail:
        """Return one document (with content) or raise NotFoundError."""
        document = await self._get_or_raise(doc_id, tenant_id=tenant_id)
        return DocumentReadDetail.model_validate(document)

    async def list_documents(
        self,
        *,
        tenant_id: UUID,
        cursor: str | None = None,
        limit: int = 20,
        tags: Sequence[str] | None = None,
    ) -> DocumentPage:
        """Keyset-paginated listing, optionally filtered by tag membership.

        Multiple tags AND together: a document is listed only when it carries
        every requested tag. Requested tags go through the same normalization
        as stored tags (trim, lowercase, dedupe); an empty/absent filter
        lists everything. The listing is scoped to `tenant_id`.
        """
        decoded = decode_id_cursor(cursor) if cursor is not None else None
        normalized_tags = _normalize_tags(tags) if tags else None
        rows = await self._repo.list_page(
            tenant_id=tenant_id, cursor=decoded, limit=limit, tags=normalized_tags
        )

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_id_cursor(last.id)

        return DocumentPage(
            items=[DocumentRead.model_validate(row) for row in rows],
            next_cursor=next_cursor,
        )

    async def update_document(
        self, doc_id: UUID, payload: DocumentUpdate, *, tenant_id: UUID
    ) -> DocumentRead:
        """Apply a partial update; content changes re-derive title and tags.

        A write reindexes (status reset to PENDING + enqueue) iff something
        index-relevant changed OR the document is not DONE yet — re-saving a
        pending/failed document is the retry path. The content-hash guard
        skips byte-identical saves: content is unchanged when its SHA-256
        matches the stored `content_hash` (a NULL hash — pre-backfill row —
        counts as changed) AND the resolved title is unchanged (title feeds
        the search index; tags are a pure function of content, so the hash
        covers them) AND `index_status` is DONE.
        """
        document = await self._get_or_raise(doc_id, tenant_id=tenant_id)
        reindex = True

        if payload.content is not None:
            title, tags = _parse_front_matter(payload.content, payload.title)
            new_hash = _content_hash(payload.content)
            content_unchanged = document.content_hash == new_hash
            title_unchanged = title == document.title
            if content_unchanged and title_unchanged and document.index_status is IndexStatus.DONE:
                reindex = False  # identical content and title on an indexed document
            else:
                document.content = payload.content
                document.title = title
                document.tags = tags
                document.content_hash = new_hash
        elif payload.title is not None:
            resolved_title = payload.title.strip() or "Untitled"
            if resolved_title == document.title and document.index_status is IndexStatus.DONE:
                reindex = False  # title-only touch that resolves to no change
            else:
                document.title = resolved_title
        elif document.index_status is IndexStatus.DONE:
            reindex = False  # empty payload cannot change an indexed document

        if reindex:
            document.index_status = IndexStatus.PENDING
        document = await self._repo.update(document)
        await self._session.commit()
        logger.info(
            "document_updated",
            document_id=str(document.id),
            title=document.title,
            reindexed=reindex,
        )
        if reindex:
            self._enqueue_indexing(document.id, document.tenant_id, document.updated_at)
        await self._bump_search_epoch()
        return DocumentRead.model_validate(document)

    async def delete_document(self, doc_id: UUID, *, tenant_id: UUID) -> None:
        """Soft-delete a document; it disappears from every read path."""
        document = await self._get_or_raise(doc_id, tenant_id=tenant_id)
        await self._repo.soft_delete(document)
        await self._session.commit()
        logger.info("document_deleted", document_id=str(document.id))
        await self._bump_search_epoch()

    async def _get_or_raise(self, doc_id: UUID, *, tenant_id: UUID) -> Document:
        document = await self._repo.get_by_id(doc_id, tenant_id=tenant_id)
        if document is None:
            # One non-leaky 404 for both "does not exist" and "another
            # tenant's document" (error-handling spec).
            raise NotFoundError(f"Document {doc_id} not found")
        return document
