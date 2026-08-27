"""Async engine, session factory, and the declarative Base — the only place
engines are constructed (see database-guidelines.md)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

# Stable constraint names so Alembic autogen produces predictable revisions.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model; models import this, never define one."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(database_url: str, **engine_kwargs: Any) -> AsyncEngine:
    """Create the async engine; `pool_pre_ping` always on."""
    return create_async_engine(database_url, pool_pre_ping=True, **engine_kwargs)


# Module-level default instance. Engines connect lazily, so importing this
# module never touches the database; tests may build their own via build_engine.
engine: AsyncEngine = build_engine(get_settings().DATABASE_URL)

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """One session per request; rollback-on-exception comes from the context."""
    async with SessionFactory() as session:
        yield session
