# Design: Indexing Pipeline

## Data model

`models/document_chunk.py` per the canonical shape in
database-guidelines.md:

```python
class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_doc_idx"),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(EMBEDDING_DIM))  # from Settings? see note
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

Notes:

- `Vector(1536)` — dimension is a spec constant, not a runtime setting;
  the `EMBEDDING_DIM` Settings field stays for provider calls, the column
  stays literal 1536 (changing it is a dedicated migration, per spec).
- Migration `0003` imports `pgvector.sqlalchemy.Vector` in the revision
  file. No new enums → no enum-lifecycle trap, but downgrade must drop
  the HNSW index explicitly before the table (index lives on the table;
  `drop_table` covers it — keep `op.drop_table` only, verify round trip).
- Soft-deleted documents' chunks are NOT purged on delete in this task
  (pipeline only ever processes pending docs; delete leaves status
  untouched — stale chunks are invisible to retrieval once retrieval
  filters by live documents; noted as known simplification).

## Chunking (`rag/chunker.py`)

Pure function `chunk_markdown(body: str, *, target: int = 800, max_size: int = 1600) -> list[str]`.

Algorithm (deterministic, no regex on arbitrary markdown):

1. Split the source into lines; drop the front-matter block
   (`---` fenced) if present — the caller may also pass already-stripped
   body; chunker itself must be front-matter-safe.
2. Group lines into sections: a new section starts at an ATX heading
   (`^#{1,6} `). The heading line travels with its section.
3. Pack consecutive sections greedily into chunks while
   `len(chunk) + len(section) <= max_size`, aiming near `target` —
   a section boundary is always preferred over splitting mid-section.
4. A single section longer than `max_size` is split at blank-line
   (paragraph) boundaries, then hard-split at line boundaries if a
   single paragraph still exceeds `max_size`.
5. Whitespace-only sections are dropped; result never contains empty
   strings; an empty body yields `[]` (a front-matter-only document
   legitimately has zero chunks — `done` with 0 chunks is honest).

`markdown-it-py` is NOT used in v1: line-based ATX splitting covers this
KB's authoring style (hand-written notes); pulling a parser in adds a
dependency surface for no current gain. Revisit when code blocks or list
nesting start confusing the splitter. (Deviation from the original
"markdown-aware via markdown-it-py" sketch — recorded here.)

## Embedding provider (`llm/embeddings.py`)

```python
class EmbeddingProvider(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

class OpenAIEmbeddingProvider:
    # AsyncOpenAI(base_url=..., api_key=...); client with timeout=60, max_retries=2
    # embed_texts: one request per call, returns in input order
```

- Raises `LLMProviderError` (existing AppError subclass) on API failure
  after SDK retries; `LLMRateLimitedError` when the SDK surfaces 429.
- Batch cap: callers pass already-sized batches; provider does not
  re-batch (indexer chunks per document — max ~dozens of chunks).

## ES store (`search/es.py`)

- `get_es_client(settings)` → `AsyncElasticsearch(hosts=[...])`.
- `ensure_index(client, name)`: create if missing with explicit mapping:
  `document_id` (keyword), `title` (text), `tags` (keyword), `chunk_text`
  (text), `chunk_index` (integer); explicit > dynamic mapping (schema
  drift becomes visible).
- `replace_document_chunks(client, index, document_id, chunks)`:
  `delete_by_query` (`term: document_id`, `conflicts=proceed`) then
  `bulk` index with deterministic ids `f"{document_id}:{i}"` —
  idempotent replace, no orphan docs on shrink.
- Index name from new Settings field `ES_INDEX: str = "kb_documents"`.

## Pipeline (`rag/indexer.py`)

```python
async def run_indexing(doc_id: UUID) -> None:   # BackgroundTasks target
    # own session per run (request session is closed by now)
    async with SessionFactory() as session:
        await IndexingPipeline(session, provider, es).process_document(doc_id)

class IndexingPipeline:
    async def process_document(self, doc_id: UUID) -> IndexStatus: ...
```

`process_document` ordering (idempotent, `done` = both stores ready):

1. Load document via `DocumentRepository.get_by_id`; missing/soft-deleted
   → log info, return (background race after delete).
2. `chunks = chunk_markdown(strip_front_matter(content))`;
   `vectors = provider.embed_texts(chunks)` — failure ⇒ `_mark_failed`.
3. PG transaction: `DocumentChunkRepository.replace_for_document`
   (delete where document_id + bulk insert) — commit. Chunks are staging;
   status untouched.
4. ES: `replace_document_chunks` — failure ⇒ `_mark_failed`.
5. PG transaction: `DocumentRepository.set_index_status(doc_id, DONE)` —
   commit. Log `document_indexed` (doc id, chunk_count, duration_ms).

`_mark_failed`: separate small tx `set_index_status(FAILED)`, warn log
with error class + doc id, **swallow** — never propagate out of the
background entry point. New repo methods on `DocumentRepository`:
`set_index_status` (one UPDATE, no ORM load); on
`DocumentChunkRepository`: `replace_for_document`.

Documents with zero chunks: steps 3–4 still run (replace = clear old),
status → `done`. Update path: status reset to `pending` already happens
in DocumentService; the enqueued run re-processes — replace semantics
make it safe.

## Write-path trigger (services stay framework-free)

- `DocumentService.__init__(self, session, enqueuer: ReindexEnqueuer | None = None)`;
  `ReindexEnqueuer = Callable[[UUID], None]` defined in
  `services/document.py`. `None` ⇒ no-op (keeps every existing test and
  CLI/service caller unchanged).
- After a successful create/update commit: `self._enqueue(document.id)`
  — enqueue AFTER commit (a rolled-back write must not index).
- `api/deps.py`: `get_document_service(session, background_tasks: BackgroundTasks)`
  returns `DocumentService(session, enqueuer=lambda doc_id: background_tasks.add_task(run_indexing, doc_id))`.
  The only place BackgroundTasks is imported — router signature gains
  nothing; FastAPI injects `BackgroundTasks` into the dependency.

## CLI (`src/app/cli.py`)

argparse (stdlib — no new dependency), subcommand `reindex`:

- `--status pending|failed` (repeatable, default both), `--limit N`
  (default 50, bounds one run), `--include-done` NOT offered (explicit
  non-goal: full rebuild is a later ops feature).
- Loops `DocumentRepository.list_by_index_status(status, limit)` (new
  keyset-able method, simple `ORDER BY created_at` + limit suffices —
  bounded by `--limit`), runs the same `IndexingPipeline`, prints
  `processed=N done=M failed=K`, exit code 0 even with failures (batch
  semantics; CI/scripting reads the counts), non-zero only on usage/db
  errors.
- Runs its own event loop entry: `asyncio.run(main())`.

## Settings

Add: `ES_INDEX: str = "kb_documents"`. Everything else already present
(`ELASTICSEARCH_URL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`,
`EMBEDDING_MODEL`, `EMBEDDING_DIM`).

## Error handling & logging

- Provider failures: `LLMProviderError`/`LLMRateLimitedError` from
  `llm/`; ES failures wrapped as `SearchIndexError` (new AppError
  subclass in core/exceptions.py, `code="search_index_error"`) — indexer
  catches broad `Exception` at the boundary anyway (a background task
  must not crash), the typed classes keep the log classification clean.
- Events: `document_indexed` (info), `document_index_failed` (warning:
  document_id, error_class — no content, no chunk text), `reindex_batch`
  (info: counts). Provider alias logged, never base_url/key.

## Tests

| Suite | Fixture world | Notes |
|---|---|---|
| `test_chunker.py` | pure, offline | boundaries, merge, oversize split, front-matter strip, empty → [] |
| `test_embeddings.py` | fake provider + protocol | error mapping; live impl under `live_llm` (excluded by default) |
| `test_indexer.py` | `db` marker, fake provider (hash-seeded 1536-dim deterministic vectors) | done path, replace-on-update, embed-fail → failed + retry → done, zero-chunk doc, deleted doc no-op |
| `test_es_store.py` | `es` marker + 1s probe → skip offline | ensure_index idempotent, replace shrinks orphans |
| `test_documents_service.py` | enqueue capture fake | create/update enqueues once post-commit; failed write does not |
| `test_cli.py` | function-level, db marker | reindex flips pending/failed, `--limit` bounds |

- conftest gains: `es` marker registration + memoized `_es_reachable()`
  probe (mirror of PG probe); `FakeEmbeddingProvider` shared fixture.
- ES tests use the same disposable pattern: unique index per test run
  (`kb_documents_test-{uuid}`) + delete at teardown — never touch a dev
  index.

## Tradeoffs / Rejected

- **markdown-it-py chunking** (deferred): line-based ATX splitting first;
  parser when real documents demand it.
- **Chunk embedding overlap** (rejected for v1): deterministic no-overlap
  chunks first; overlap is a retrieval-quality knob for the search task
  to evaluate with real data.
- **ARQ worker now** (rejected): spec says BackgroundTasks first; the
  pipeline is already process-scoped and CLI-retriggerable, so the ARQ
  migration only swaps the enqueue adapter.
- **Transactional outbox** (rejected): single-user MVP; `--status failed`
  CLI sweep + `pending` never-drains invariant is enough. Revisit with
  real concurrency.
- **Chunk-level partial update** (rejected): whole-document replace is
  simpler and idempotent; docs are small.

## Rollback

Two commits (model+migration / pipeline+trigger+cli+tests); `alembic
downgrade -1` drops `document_chunks`; ES index is disposable
(delete_index); revert restores pure-CRU documents slice behavior.
