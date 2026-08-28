# Implement: Documents Vertical Slice

Ordered checklist. Gates after each group; commit points G1/G2.

## G1 — Model + migration

- [ ] `src/app/models/__init__.py` (re-export for Alembic autogen + imports)
- [ ] `src/app/models/document.py` — Document + IndexStatus per design.md
- [ ] `alembic/env.py`: ensure `app.models` imported so metadata registers
- [ ] `uv run alembic revision --autogenerate -m "documents table"` → edit:
      verify GIN index on tags, keyset index, index_status server default,
      ARRAY server_default, working downgrade
- Validation: `uv run alembic upgrade head` then `downgrade -1` then
  `upgrade head` against compose PG; `uv run mypy src`; `uv run ruff check .`

**Commit G1** — "feat: documents model + migration"

## G2 — Schemas, repository, service, endpoints

- [ ] `schemas/document.py` — Create/Update/Read/ReadDetail/Page per design
- [ ] `repositories/document.py` — create/get_by_id/list_page/update/
      soft_delete; keyset via `tuple_()`, `tag = ANY(tags)`, limit+1 trick
- [ ] `services/document.py` — front-matter parse + normalization + title
      fallback + index_status reset on update; commits; raises
      NotFoundError/ValidationError
- [ ] `api/v1/endpoints/documents.py` — five routes, thin; status codes per
      design table
- [ ] register router in `api/v1/router.py`
- Validation: `uv run ruff check . && uv run ruff format . && uv run mypy src`

## G3 — Test infrastructure + tests

- [ ] conftest: `db` marker, KB_TEST_DATABASE_URL session fixture
      (maintenance-DB create + create_all + truncate per test),
      reachability probe → skip, `get_db` override on client fixture
- [ ] `tests/test_documents_api.py` — CRUD status codes, 404/422 envelopes,
      content excluded from list, detail includes content
- [ ] `tests/test_documents_service.py` — front-matter title/tags
      extraction + fallback, invalid tags/yaml → ValidationError,
      normalization (strip/lower/dedupe), tag filter, pagination
      disjoint pages + ordering, soft-delete invisibility, update resets
      index_status
- Validation:
  - compose PG up → `uv run pytest` (all run, none skipped)
  - `docker compose stop postgres` → `uv run pytest` green with skips →
    `docker compose start postgres`
  - full gates; manual curl smoke of one create + list against uvicorn
    (optional, tests are primary evidence)

**Commit G2+G3** — "feat: documents CRUD, front matter, tags, pagination + db test infra"

## Review gates

- trellis-check dispatch after G3: all five backend spec files, PRD
  acceptance sweep, gates re-run.

## Rollback

- After G1: `alembic downgrade -1` + `git revert <g1>`
- After G3: `git revert <g2g3>`; `kb_test` disposable
