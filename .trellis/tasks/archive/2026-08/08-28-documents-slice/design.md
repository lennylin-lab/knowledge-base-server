# Design: Documents Vertical Slice

## Data model

```python
class IndexStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="Untitled")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    index_status: Mapped[IndexStatus] = mapped_column(
        SAEnum(IndexStatus, name="index_status"), nullable=False,
        server_default=IndexStatus.PENDING.value,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)  # reserved
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Migration `0002_documents`:
- table per above (names via Base naming conventions)
- GIN index on `tags` (`ix_documents_tags`, `postgresql_using="gin"`)
- index on `(created_at DESC, id DESC)` for keyset pagination
- downgrade drops the table

### Design decision: tags as `ARRAY(Text)`, not a join table

- **Context**: tags need filtering (`?tag=`), future rename/across-doc ops.
- **Options**: (a) `ARRAY(Text)` + GIN; (b) `tags` + `document_tags` tables.
- **Decision**: (a). Tags originate in front matter — document-owned data,
  no independent tag entity lifecycle in MVP. Array ops (`array_replace`)
  cover future tag rename; GIN covers membership queries. A join table buys
  nothing until tags gain their own metadata (counts as separate feature).
- **Extensibility**: migrating ARRAY → join table later is one migration.

### index_status lifecycle

Writes always set `PENDING` (create) or reset to `PENDING` (update) —
content changed ⇒ re-index needed. The pipeline task (later) flips to
`DONE`/`FAILED`. This task never sets `DONE`.

## Front matter contract

- `python-frontmatter.parse(content)` → `Post`; `post.metadata` dict.
- `title`: metadata `title` if `isinstance(str)` and non-empty → else
  request `title` → else `"Untitled"`.
- `tags`: metadata `tags` must be `list[str]` (or absent → `[]`);
  non-list or non-str items → `ValidationError` (422, code
  `validation_failed`, details name the field). Tags normalized:
  strip, lower, drop empties, dedupe (preserve order).
- Malformed YAML → `ValidationError` with message from the parser error
  (no traceback leak).
- `content` is stored **verbatim** (front matter included) — it is the
  source of truth; columns are derived read models.

## API

| Method | Path | Success | Errors |
|--------|------|---------|--------|
| POST | `/api/v1/documents` | 201 + `DocumentRead` | 422 invalid body/front matter |
| GET | `/api/v1/documents?cursor&limit&tag` | 200 + `DocumentPage` | — |
| GET | `/api/v1/documents/{id}` | 200 + `DocumentRead` | 404 `not_found` |
| PATCH | `/api/v1/documents/{id}` | 200 + `DocumentRead` | 404, 422 |
| DELETE | `/api/v1/documents/{id}` | 204 | 404 |

`DocumentCreate`: `content: str` (required, non-empty), `title: str | None`.
`DocumentUpdate`: partial (`content`, `title`; at least one field — else 422).
`DocumentRead`: id, title, tags, index_status, created_at, updated_at —
**content excluded from lists** (payload bloat); included only in single GET
via `DocumentReadDetail` (Read + content).
`DocumentPage`: `items: list[DocumentRead]`, `next_cursor: str | None`.

## Keyset pagination

- Order: `created_at DESC, id DESC` (tie-break deterministic).
- Cursor = urlsafe-base64(json `{"ca": iso, "id": uuid}`).
- `list_page(cursor=None, limit=20, tag=None)`:
  `WHERE deleted_at IS NULL [AND :tag = ANY(tags)]
   [AND (created_at, id) < (:ca, :id)] ORDER BY ... LIMIT :limit + 1`;
  the extra row decides `next_cursor`. Row-value comparison
  `(a, b) < (x, y)` is a PG feature — use `tuple_()` in SQLAlchemy.
- `limit` clamp 1..100. Invalid cursor → 422 `ValidationError`.
- Note: same-`created_at` collisions across docs are handled by the id
  tie-break; server-default `func.now()` has microsecond precision.

## Soft delete

`soft_delete` sets `deleted_at = func.now()`. Repository queries filter
`deleted_at IS NULL`. Hard delete never exposed via API in MVP.

## Test infrastructure (`tests/conftest.py`)

- `KB_TEST_DATABASE_URL` (default
  `postgresql+asyncpg://kb:kb@localhost:5432/kb_test`).
- Session-scoped fixture: connect to the `postgres` maintenance DB on the
  same host, `CREATE DATABASE kb_test` (ignore DuplicateDataBase), then
  `create_all` on the test DB. Function-scoped: truncate all tables
  (`TRUNCATE ... CASCADE`) between tests.
- Reachability probe with 1s timeout before the session fixture; on failure
  → `pytest.skip` for every `db`-marked test with reason "PG not reachable"
  — keeps offline `uv run pytest` green.
- `client` fixture gains a `get_db` dependency override pointing at the
  test session factory (app factory dependency_overrides).
- Tests marked `@pytest.mark.db`; marker registered in pyproject.

## Reuse / placement

- No new top-level modules beyond spec layout: `models/document.py`,
  `schemas/document.py`, `repositories/document.py`,
  `services/document.py`, `api/v1/endpoints/documents.py` registered in
  `api/v1/router.py`.
- `utils/` gains nothing — front-matter parsing lives in the service
  (business rule), not a util (no second consumer yet).

## Tradeoffs / Rejected

- Offset pagination (rejected): spec forbids OFFSET for collections.
- Separate `document_tags` join (rejected): see design decision above.
- Returning `content` in list responses (rejected): KB documents are big;
  lists are for navigation.
- TypeORM-style bidirectional relations (n/a): no relations yet.
- Hard delete (rejected): knowledge loss is unacceptable in a KB; soft
  delete is cheap insurance.

## Rollback

Two commits (model+migration / service+api+tests);
`git revert` per commit; `alembic downgrade -1` drops the table;
`kb_test` DB is disposable.
