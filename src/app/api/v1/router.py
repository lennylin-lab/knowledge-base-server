"""Aggregates all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints.chat import router as chat_router
from app.api.v1.endpoints.documents import router as documents_router
from app.api.v1.endpoints.search import router as search_router
from app.api.v1.endpoints.writing import router as writing_router

api_v1_router = APIRouter()

api_v1_router.include_router(documents_router, prefix="/documents", tags=["documents"])
api_v1_router.include_router(search_router, prefix="/search", tags=["search"])
api_v1_router.include_router(chat_router, prefix="/chat", tags=["chat"])
api_v1_router.include_router(writing_router, prefix="/writing", tags=["writing"])
