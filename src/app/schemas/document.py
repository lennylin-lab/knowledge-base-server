"""Document request/response DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.document import IndexStatus


class DocumentCreate(BaseModel):
    """Create a document from raw markdown (front matter included)."""

    content: str = Field(min_length=1)
    title: str | None = None


class DocumentUpdate(BaseModel):
    """Partial update; at least one field must be provided."""

    content: str | None = Field(default=None, min_length=1)
    title: str | None = None
    # Optimistic-concurrency guard: when provided, the write is rejected with
    # 409 unless it matches the stored content_hash. Absent = last-write-wins.
    # It is a guard, not a change: it never satisfies the one-field rule.
    expected_content_hash: str | None = None

    @model_validator(mode="after")
    def _require_one_field(self) -> Self:
        if self.content is None and self.title is None:
            raise ValueError("At least one field must be provided")
        return self


class DocumentRead(BaseModel):
    """Document as returned in lists — content excluded (payload bloat)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    tags: list[str]
    index_status: IndexStatus
    created_at: datetime
    updated_at: datetime


class DocumentReadDetail(DocumentRead):
    """Single-document view: everything in `DocumentRead` plus content."""

    content: str
    # NULL = pre-backfill row whose hash was never computed ("unknown");
    # clients should skip concurrent-write validation in that case.
    content_hash: str | None


class DocumentPage(BaseModel):
    """One page of a keyset-paginated document list."""

    items: list[DocumentRead]
    next_cursor: str | None
