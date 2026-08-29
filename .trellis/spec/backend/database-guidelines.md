# Database Guidelines

> ORM patterns, queries, migrations for this project.

---

## Overview

- **Database**: PostgreSQL 16+ with the **pgvector** extension for RAG
  embeddings. Vector column dimension is fixed at **1536**
  (OpenAI `text-embedding-3-small`-class models via the OpenAI-compatible
  endpoint); changing it later is a new-column + backfill migration, decided
  explicitly, never casually.
- **ORM**: SQLAlchemy 2.0 async (`asyncpg` driver), Mapped/mapped_column
  declarative style only. Vector columns use `pgvector.sqlalchemy.Vector`.
- **Full-text BM25 lives in Elasticsearch, not PG** — PG stores relational +
  vector data only (see `search/` in directory-structure.md).
- **Migrations**: Alembic, async template. Schema changes never happen via
  `create_all()` outside tests. Extension creation
  (`CREATE EXTENSION IF NOT EXISTS vector`) ships as the first migration and
  requires the extension available in the PG image (dev + CI).

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
  append-only tables. The uuid4 default is **client-side** (SQLAlchemy
  `default=`, not `server_default`) — raw psql/ops inserts must supply `id`
  explicitly.
- Timestamps: `DateTime(timezone=True)` — never naive datetimes.
  `created_at`/`updated_at` via `server_default=func.now()`.
- All foreign keys are explicit `ForeignKey(...)` with `ondelete=` specified.
- Enums: `StrEnum` classes mapped with `SAEnum`; never bare string columns
  for closed sets.
- **Ownership columns reserved**: even in the single-user MVP, documents
  carry an optional `owner_id` column so the multi-user upgrade is a
  migration + auth change, not a schema redesign.

### Embeddings / pgvector models

```python
# models/document_chunk.py (canonical shape)
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID

class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1536))  # dimension fixed, see Overview
```

Rules:

- One row per chunk; `(document_id, chunk_index)` has a unique constraint.
- Re-embedding a document deletes and re-inserts its chunks in one
  transaction (chunks are derived data, never updated in place).
- Embeddable tables keep an indexing status column on the parent
  (`documents.index_status`: `pending | done | failed`) so the background
  pipeline is observable and retryable.

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

Vector similarity lives in a repository too (`rag/` orchestrates, the
repository executes SQL):

```python
# repositories/document_chunk.py (canonical shape — implemented in hybrid-retrieval)
# Retrieval reads JOIN live documents: PG is the single visibility source
# of truth — soft-deleted documents' chunks never surface, regardless of
# what ES still indexes (ES ranks, PG hydrates).
async def search_similar(
    self, embedding: list[float], *, limit: int, tag: str | None = None
) -> Sequence[ChunkRow]:   # hydrated: chunk cols + document title/tags
    stmt = (
        select(_LIVE_CHUNK_SELECT)          # chunk + document columns
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.deleted_at.is_(None))   # + AND :tag = ANY(Document.tags)
        .order_by(DocumentChunk.embedding.cosine_distance(embedding))
        .limit(limit)
    )
    ...

async def get_live_chunks(
    self, keys: Sequence[tuple[UUID, int]]
) -> dict[tuple[UUID, int], ChunkRow]:
    # tuple-IN hydration for ES-leg keys (document_id, chunk_index);
    # bounded by the candidate pool; keys of deleted docs hydrate to
    # nothing and are dropped by the caller
```

Rule: **retrieval queries always join live documents**
(`deleted_at IS NULL`). Visibility filtering never lives in ES.

Hybrid retrieval (`rag/retriever.py`) runs ES BM25 and pgvector searches
concurrently, then fuses ranks with Reciprocal Rank Fusion (RRF) in Python —
SQL/ES return candidate lists, fusion is plain code and unit-testable.

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

## Elasticsearch (`search/`)

All ES access lives in `search/` — same rule as repositories for SQL.
Canonical replace pattern from `rag/indexer.py` + `search/es.py`:

- **Explicit mapping** on index create (`ensure_index`): `document_id`
  keyword, `title` text, `tags` keyword, `chunk_text` text, `chunk_index`
  integer. Never rely on dynamic mapping — schema drift must be visible.
- **Idempotent replace**: delete-by-query (`term: document_id`,
  `conflicts="proceed"`) then bulk-index with **deterministic ids**
  `f"{document_id}:{chunk_index}"`. A shrink leaves no orphans; a re-run
  is always safe.
- > **Warning: near-real-time visibility.** By default ES makes bulked docs
  > visible only at the next refresh (~1s). A `delete_by_query` issued in
  > the same pipeline run can miss docs bulk-indexed moments earlier —
  > re-indexing then leaks orphans. Pass `refresh=True` on **both** the
  > delete and the bulk write when the pipeline reads-back or replaces
  > within one run. Correctness over write amplification for the MVP.
- elasticsearch-py 8.x exposes **no common exception base**: catch
  `(ApiError, TransportError, BulkIndexError)` and wrap into
  `SearchIndexError` with operation/index/error_class details.

## Migrations (Alembic)

- One revision per logical change; message format:
  `add_document_chunks table` style lowercase description.
- Generate with autogen against `models/` metadata, then **read and edit the
  generated file** — autogen misses constraint renames and server defaults.
- Every revision must have a **downgrade** that actually reverses the change.
- Destructive changes (drop column/table) ship as two steps: deploy code that
  stops writing → later revision drops.

### Enum type lifecycle (the autogen trap)

Autogenerate emits **broken** migrations for any model column using `SAEnum`
on a PG `ENUM` type. The naive form breaks `upgrade → downgrade -1 → upgrade`
with a `DuplicateObjectError` on the second upgrade. Three things conspire:

1. `op.create_table`'s implicit `CREATE TYPE` is **not `checkfirst`-guarded** —
   if a downgrade left the type behind (or the type pre-exists), the upgrade
   crashes. Prevent this by creating the type **explicitly** before the table:
   `sa.Enum("pending", "done", "failed", name="...").create(op.get_bind(), checkfirst=True)`.
2. The generic `sa.Enum(..., create_type=False)` in the column is **silently
   ignored** — SQLAlchemy drops "backend-inapplicable kwargs" with no warning,
   so the implicit `CREATE TYPE` still fires inside `create_table`. Use the
   **PG-specific** `postgresql.ENUM(..., create_type=False)` in the column so
   the column does not re-emit the type the explicit step already made.
3. The downgrade must **explicitly drop** the type with `checkfirst=True`
   (`sa.Enum(name="...").drop(op.get_bind(), checkfirst=True)`); relying on the
   implicit drop from `drop_table` is not symmetric with the explicit create.

Canonical shape (from `0002_documents`):

```python
def upgrade() -> None:
    sa.Enum("pending", "done", "failed", name="index_status").create(
        op.get_bind(), checkfirst=True
    )
    op.create_table(
        "documents",
        ...
        sa.Column(
            "index_status",
            postgresql.ENUM("pending", "done", "failed", name="index_status", create_type=False),
            server_default="pending",
            nullable=False,
        ),
        ...
    )

def downgrade() -> None:
    op.drop_index(...)            # drop GIN/other indexes first
    op.drop_table("documents")
    sa.Enum(name="index_status").drop(op.get_bind(), checkfirst=True)
```

Also keep the `SAEnum` `values_callable=lambda e: [m.value for m in e]` on the
**model** so the persisted values (`"pending"`) — not the member names
(`"PENDING"`) — match the `server_default`. Verify the round trip with
`alembic upgrade head` → `alembic downgrade -1` → `alembic upgrade head` and a
`pg_type` check between steps; any `StrEnum`/`SAEnum` migration that does not
follow this shape will fail it.

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
- Embedding calls (`llm/embeddings.py`) inside repositories — repositories
  store/search vectors; only `rag/` and `llm/` call providers.
- Full-text search via PG `tsvector` for user-facing search — BM25 relevance
  is Elasticsearch's job.
- Changing `Vector(1536)` dimension without a dedicated migration plan.
