# Database Guidelines

> ORM patterns, queries, migrations for this project.

---

## Overview

- **Database**: PostgreSQL 16+ (pgvector extension allowed for embeddings).
- **ORM**: SQLAlchemy 2.0 async (`asyncpg` driver), Mapped/mapped_column
  declarative style only.
- **Migrations**: Alembic, async template. Schema changes never happen via
  `create_all()` outside tests.

---

## Setup & Session Management

Engine and session live in `core/database.py` — never create engines elsewhere.

```python
# core/database.py (canonical shape)
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
```

Rules:

- `get_db` is the **only** way routers/services obtain a session
  (`Depends(get_db)`); pass it down explicitly — no session globals.
- One session per request. Services doing multi-step writes use a single
  transaction: commit once at the end, rely on rollback-on-exception from the
  `async with` context.
- `expire_on_commit=False` always — never re-touch attributes after commit
  in async code.

## Models (SQLAlchemy 2.0 style)

```python
# models/document.py (canonical shape)
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

Rules:

- Every model subclasses `Base` (from `core/database.py`); `Base` sets
  `__mapper_args__` conventions and the table naming convention below.
- **Naming**: table names are `snake_case` plural (`documents`,
  `document_chunks`); columns are `snake_case`.
- Primary keys: UUID (`uuid4`) by default; bigserial only for high-write
  append-only tables.
- Timestamps: `DateTime(timezone=True)` — never naive datetimes.
  `created_at`/`updated_at` via `server_default=func.now()`.
- All foreign keys are explicit `ForeignKey(...)` with `ondelete=` specified.
- Enums: `StrEnum` classes mapped with `SAEnum`; never bare string columns
  for closed sets.

## Query Patterns

Repositories are the only layer that queries. 2.0-style `select()` only:

```python
# repositories/document.py (canonical shape)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, doc_id: UUID) -> Document | None:
        stmt = select(Document).where(Document.id == doc_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_recent(self, limit: int = 50) -> sequence[Document]:
        stmt = select(Document).order_by(Document.created_at.desc()).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()
```

Rules:

- **Always async** (`AsyncSession`); sync engines are forbidden in `src/`.
- Use `select()` + `.execute()`; the legacy `session.query()` API is
  **forbidden**.
- Exactly-one expectations: `scalar_one()` / `scalar_one_or_none()` —
  never `scalars().first()` when uniqueness is guaranteed.
- `.limit()`/`.offset()` for pagination; wrap in a cursor (keyset) pattern
  for large collections — avoid `OFFSET` on big tables.
- N+1: use `selectinload()` for to-many relations, `joinedload()` sparingly
  for to-one. Lazy loading in async is a bug — if an attribute access can
  trigger SQL, load it eagerly in the repository.
- SQL string escapes (`text()` with f-strings) are forbidden unless the
  statement cannot be expressed in the ORM; if unavoidable, bind parameters.

## Migrations (Alembic)

- One revision per logical change; message format:
  `add_document_chunks table` style lowercase description.
- Generate with autogen against `models/` metadata, then **read and edit the
  generated file** — autogen misses constraint renames and server defaults.
- Every revision must have a **downgrade** that actually reverses the change.
- Destructive changes (drop column/table) ship as two steps: deploy code that
  stops writing → later revision drops.

```bash
uv run alembic revision --autogenerate -m "add documents table"
uv run alembic upgrade head
```

---

## Forbidden Patterns

- `session.query(...)` — legacy Query API.
- Sync `create_engine` / `psycopg2` in application code.
- Business logic in repositories (validation, HTTP errors, calculations).
- Committing inside repositories — transaction boundaries belong to services.
- `Base.metadata.create_all()` outside test fixtures.
