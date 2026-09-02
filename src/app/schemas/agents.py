"""Agent-result response DTOs (computed on demand, never persisted)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class SummaryResult(BaseModel):
    """One document summary as returned by the summarize endpoint."""

    document_id: UUID
    summary: str
    model: str
    latency_ms: float


class AssociationItem(BaseModel):
    """One related document: deterministic candidate metadata plus the
    LLM-written reason. Every field except `reason` comes from the candidate
    set gathered before the run — the model only selects and explains."""

    document_id: UUID
    title: str
    tags: list[str]
    reason: str
    signal: str


class AssociationsResult(BaseModel):
    """Related documents as returned by the associations endpoint."""

    document_id: UUID
    associations: list[AssociationItem]
    model: str
    latency_ms: float
