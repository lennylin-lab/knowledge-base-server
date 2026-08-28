# Documents Vertical Slice: Model, CRUD, Tags, Front Matter

## Goal

First business vertical of the knowledge base: Markdown documents with
front-matter-driven metadata (title, tags), full CRUD over REST, backed by
PostgreSQL. Establishes the reference implementation shape that later tasks
(chunks/indexing, agents, search) plug into.

No ES indexing, no embeddings, no agents in this task — writes only mark
`index_status = pending` for the future pipeline.

## Requirements

1. `Document` model (`models/document.py`) + Alembic revision `0002`:
   UUID pk, `title`, `content` (raw markdown incl. front matter),
   `tags` array, `index_status`, `owner_id` (nullable, reserved),
   `created_at`/`updated_at` (tz-aware, server defaults), `deleted_at`
   (soft delete, nullable).
2. On create/update the service parses YAML front matter from `content`
   (`python-frontmatter`) and persists `title` and `tags` into columns.
   Title resolution order: front-matter `title` → request `title` →
   `"Untitled"`. Tags come only from front matter; invalid YAML front
   matter → 422 `ValidationError`.
3. Repository (`repositories/document.py`): `create`, `get_by_id`,
   `list_page` (keyset pagination + optional tag filter, excludes soft
   deleted), `update`, `soft_delete`. No business logic, no commits.
4. Service (`services/document.py`): orchestrates parsing + repo calls,
   raises `NotFoundError`/`ValidationError`; transactions commit here.
5. Schemas (`schemas/document.py`): `DocumentCreate`, `DocumentUpdate`
   (partial), `DocumentRead`, `DocumentPage` (items + next_cursor).
6. Endpoints (`api/v1/endpoints/documents.py`, prefix `/api/v1/documents`):
   `POST /` 201, `GET /` list (query: `cursor`, `limit` ≤100 default 20,
   `tag`), `GET /{id}` 200, `PATCH /{id}` 200, `DELETE /{id}` 204.
   Routers stay thin (no ORM, one service call).
7. DB test infrastructure in `tests/conftest.py`: dedicated `kb_test`
   database on the compose PG instance, `Base.metadata.create_all` once per
   session, truncate between tests; `db` marker auto-skips when PG is not
   reachable so the offline gate `uv run pytest` stays green.
8. Tests: API contract (status codes, envelope on 404/422), service rules
   (front-matter extraction, title fallback, soft delete invisibility,
   tag filter, pagination ordering + cursor stability), repository
   (covered via service tests per spec).

## Out of Scope

- ES indexing / chunking / embeddings (pipeline task; `index_status`
  stays `pending`)
- Full-text search endpoints
- Auth / ownership enforcement (column exists, unused)
- Bulk import, file upload (content arrives as JSON string)
- Webhook/event emission

## Acceptance Criteria

- [ ] `uv run alembic upgrade head` applies `0002` against compose PG
      (upgrade → downgrade → upgrade cycle works)
- [ ] With compose PG up: all CRUD endpoints behave per requirement 6,
      verified by tests (201/200/204, 404 envelope on missing id,
      422 envelope on invalid front matter)
- [ ] Front matter `{title: "X", tags: [a, b]}` reflects in `DocumentRead`
      and list `?tag=a` filters correctly (tested)
- [ ] Keyset pagination: second page via `next_cursor` returns disjoint
      items in `created_at desc` order (tested)
- [ ] Soft-deleted documents absent from list and 404 on direct GET (tested)
- [ ] Offline (docker down): `uv run pytest` green (db tests skip with
      visible skip reason)
- [ ] Gates green: ruff check / ruff format --check / mypy src / pytest
