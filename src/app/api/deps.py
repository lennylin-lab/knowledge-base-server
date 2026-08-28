"""Shared FastAPI dependencies for the v1 API."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services.document import DocumentService

SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_document_service(session: SessionDep) -> DocumentService:
    """One service per request, sharing the request's session."""
    return DocumentService(session)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
