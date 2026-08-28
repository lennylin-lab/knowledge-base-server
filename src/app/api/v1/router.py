"""Aggregates all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints.documents import router as documents_router

api_v1_router = APIRouter()

api_v1_router.include_router(documents_router, prefix="/documents", tags=["documents"])
