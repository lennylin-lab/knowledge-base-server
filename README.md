# knowledge-base-server

Backend for a personal knowledge base: Markdown documents in, hybrid search
(Elasticsearch BM25 + pgvector) and LLM agents out.

Stack: Python 3.12 / FastAPI (async) + PostgreSQL 16 with pgvector +
Elasticsearch 8 + Pydantic AI agents. Managed with [uv](https://docs.astral.sh/uv/).

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.11+
- Docker with the compose plugin
- Linux only: Elasticsearch needs `vm.max_map_count >= 262144`. Check with
  `cat /proc/sys/vm/max_map_count`; if lower, raise it (persists across reboots
  via sysctl):

  ```bash
  sudo sysctl -w vm.max_map_count=262144
  ```

## Quickstart

```bash
# 1. Install dependencies (creates .venv, writes/uses uv.lock)
uv sync

# 2. Local settings
cp .env.example .env   # adjust if needed; defaults match docker-compose.yml

# 3. Fetch the Elasticsearch IK analyzer plugin (version-locked to the ES
#    version pinned in docker-compose.yml), then start PostgreSQL (pgvector)
#    + Elasticsearch. The image is built locally; the plugin installs from
#    the fetched zip (infini's download mirrors can throttle to KB/s).
curl -fL --retry 5 -o docker/elasticsearch/elasticsearch-analysis-ik-8.17.3.zip \
  https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-8.17.3.zip
docker compose up -d
docker compose ps      # wait until both report healthy

# 4. Apply database migrations
uv run alembic upgrade head

# 5. Run the API
uv run uvicorn app.main:app --reload
```

Smoke test:

```bash
curl -i http://localhost:8000/healthz
curl -i http://localhost:8000/nope   # 404 with the standard error envelope
```

Interactive docs: <http://localhost:8000/docs>.

## Re-indexing after a search-index change

Analyzer and mapping changes never apply to an existing index (Elasticsearch
validates mappings, it does not re-analyze in place), and chunking changes
alter both the ES documents and the embedding inputs. After pulling a change
that touches any of these, re-index once:

```bash
# 1. Drop the index created with the old settings + mapping (recreated with
#    the current ones during the sweep below)
curl -XDELETE localhost:9200/kb_documents

# 2. Queue every already-indexed document for re-indexing
docker compose exec postgres psql -U kb -d kb -c "UPDATE documents SET index_status = 'pending' WHERE index_status = 'done';"

# 3. Sweep the queue (default limit 50 per run; repeat until `processed: 0`)
uv run python -m app.cli reindex

# 4. Confirm the corpus is back
curl -s localhost:9200/kb_documents/_count
```

The sweep is idempotent (deterministic document ids), so re-running it is
safe. Instances of this migration so far:

- **IK analyzer** (`09-06-es-ik-analyzer`): `title`/`chunk_text` switched from
  `standard` to `ik_max_word`/`ik_smart`. Also required the one-time image
  rebuild with the plugin baked in (Quickstart step 3).
- **Code-aware chunking** (`09-06-code-aware-chunking`): fenced code blocks
  stay intact, the index gains index `settings` with a `code` analyzer, a
  `chunk_text.code` subfield, and a `heading_path` field; chunks now embed
  `title + heading breadcrumb + text` (PG still stores the plain text — no
  schema change). Both legs' inputs change, so a full reindex is required.
  Afterward, recalibrate `KB_SEARCH_VECTOR_MAX_DISTANCE` with
  `tests/test_vector_distance_probe.py` if retrieval quality shifts.
- **BM25 scoring overhaul** (`09-08-es-bm25-scoring`): the `code` analyzer
  gains `remove_duplicates` (camelCase identifiers were stored twice,
  inflating BM25 tf) and the subfield splits into an index analyzer plus a
  new `code_search` search analyzer bound via `search_analyzer` — a flat
  token stream that stops identifier queries from compiling into adjacency
  phrases (they returned 0 hits). Query shape changed from a single
  `multi_match` to additive field groups with a term-coverage gate; only the
  analyzers force this reindex.

## Quality gates

All four must pass before any commit; CI runs exactly these:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Tests marked `live_llm` (real provider calls) are excluded from the default
run; include them with `uv run pytest -m live_llm --override-ini='addopts='`.

## Configuration

All settings come from the `KB_*` environment variables (or `.env`); see
`.env.example` and `src/app/core/config.py`. Nothing is hardcoded in `src/`.
External MCP tools are configured via `mcp.json` (see `mcp.json.example`);
stdio/npx-style servers are bridged to HTTP by opt-in sidecars — see
[`docs/mcp-sidecars.md`](docs/mcp-sidecars.md).

## Repository layout

- `src/app/` — the `app` package (today: `api` + `core`; models / schemas / repositories
  / services / agents / rag / mcp / search land with their tasks, per the specs)
- `alembic/` — database migrations
- `tests/` — pytest suite (offline by default)
- `.trellis/spec/backend/` — binding coding conventions for this codebase

## Development notes

- Migrations: `uv run alembic revision --autogenerate -m "add <table> table"`,
  then read and edit the generated file. Every revision needs a working
  downgrade. The URL is read from `KB_DATABASE_URL` (see `alembic/env.py`).
- Teardown of dev infra: `docker compose down` (keeps volumes) or
  `docker compose down -v` (wipes data).
