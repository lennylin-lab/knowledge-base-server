"""Shared FastAPI dependencies for the v1 API."""

from __future__ import annotations

from typing import Annotated

from fastapi import BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.rag.indexer import run_indexing
from app.services.document import DocumentService

SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_document_service(session: SessionDep, background_tasks: BackgroundTasks) -> DocumentService:
    """One service per request, sharing the request's session.

    The FastAPI adapter for the write-path indexing trigger: the service only
    sees an injected enqueuer; this is the sole place BackgroundTasks appears.
    """
    return DocumentService(
        session,
        enqueuer=lambda doc_id: background_tasks.add_task(run_indexing, doc_id),
    )


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
