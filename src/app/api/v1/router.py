"""Aggregates all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter

api_v1_router = APIRouter()

# Endpoint routers register here as vertical slices land, e.g.:
# api_v1_router.include_router(documents_router, prefix="/documents", tags=["documents"])
