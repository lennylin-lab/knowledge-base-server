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
