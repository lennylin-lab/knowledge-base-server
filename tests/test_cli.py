"""CLI: reindex sweep semantics (counts, filters, limits) + argparse surface
+ ARQ worker subcommand wiring (offline smoke)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cli
from app.core.exceptions import LLMProviderError
from app.models.document import IndexStatus
from app.rag.indexer import IndexingPipeline
from app.repositories.document import DocumentRepository
from app.schemas.document import DocumentCreate
from app.services.document import DocumentService
from fakes import RecordingEsStore, StubEsClient, hermetic_settings


def make_test_pipeline(
    session_factory: async_sessionmaker[AsyncSession], provider: object
) -> IndexingPipeline:
    es_store = RecordingEsStore()
    return IndexingPipeline(
        session_factory=session_factory,
        embedding_provider=provider,  # structurally compatible fake
        es_client=StubEsClient(),
        es_index="kb_documents_test",
        ensure_index=es_store.ensure_index,
        replace_chunks=es_store.replace_chunks,
    )


async def seed(session_factory: async_sessionmaker[AsyncSession], *contents: str) -> list[UUID]:
    ids: list[UUID] = []
    async with session_factory() as session:
        service = DocumentService(session)
        for content in contents:
            ids.append((await service.create_document(DocumentCreate(content=content))).id)
    return ids


async def statuses_of(
    session_factory: async_sessionmaker[AsyncSession], ids: list[UUID]
) -> dict[UUID, IndexStatus]:
    async with session_factory() as session:
        repository = DocumentRepository(session)
        result = {}
        for doc_id in ids:
            document = await repository.get_by_id(doc_id)
            assert document is not None
            result[doc_id] = document.index_status
        return result


async def set_status(
    session_factory: async_sessionmaker[AsyncSession], doc_id: UUID, status: IndexStatus
) -> None:
    async with session_factory() as session:
        await DocumentRepository(session).set_index_status(doc_id, status)
        await session.commit()


@pytest.mark.db
async def test_reindex_flips_pending_and_failed_to_done(session_factory, fake_embedding_provider):
    pending_id, failed_id, done_id = await seed(
        session_factory, "# A\n\ntext a", "# B\n\ntext b", "# C\n\ntext c"
    )
    await set_status(session_factory, failed_id, IndexStatus.FAILED)
    await set_status(session_factory, done_id, IndexStatus.DONE)

    counts = await cli.run_reindex(
        statuses=[IndexStatus.PENDING, IndexStatus.FAILED],
        limit=50,
        session_factory=session_factory,
        pipeline_factory=lambda: make_test_pipeline(session_factory, fake_embedding_provider),
    )

    assert counts == {"processed": 2, "done": 2, "failed": 0, "skipped": 0}
    statuses = await statuses_of(session_factory, [pending_id, failed_id, done_id])
    assert statuses[pending_id] is IndexStatus.DONE
    assert statuses[failed_id] is IndexStatus.DONE
    assert statuses[done_id] is IndexStatus.DONE  # untouched, merely already done


@pytest.mark.db
async def test_reindex_status_filter_leaves_other_statuses_alone(
    session_factory, fake_embedding_provider
):
    pending_id, failed_id = await seed(session_factory, "# A\n\ntext a", "# B\n\ntext b")
    await set_status(session_factory, failed_id, IndexStatus.FAILED)

    counts = await cli.run_reindex(
        statuses=[IndexStatus.FAILED],
        limit=50,
        session_factory=session_factory,
        pipeline_factory=lambda: make_test_pipeline(session_factory, fake_embedding_provider),
    )

    assert counts["processed"] == 1
    statuses = await statuses_of(session_factory, [pending_id, failed_id])
    assert statuses[pending_id] is IndexStatus.PENDING  # not swept
    assert statuses[failed_id] is IndexStatus.DONE


@pytest.mark.db
async def test_reindex_limit_bounds_one_run(session_factory, fake_embedding_provider):
    await seed(session_factory, "# A\n\ntext a", "# B\n\ntext b", "# C\n\ntext c")

    counts = await cli.run_reindex(
        statuses=[IndexStatus.PENDING],
        limit=2,
        session_factory=session_factory,
        pipeline_factory=lambda: make_test_pipeline(session_factory, fake_embedding_provider),
    )

    assert counts["processed"] == 2
    async with session_factory() as session:
        still_pending = await DocumentRepository(session).list_by_index_status(
            [IndexStatus.PENDING], limit=50
        )
    assert len(still_pending) == 1  # the run was bounded by --limit


@pytest.mark.db
async def test_reindex_swallows_per_document_failures(session_factory, fake_embedding_provider):
    await seed(session_factory, "# A\n\ntext a", "# B\n\ntext b")
    fake_embedding_provider.error = LLMProviderError("provider down")

    counts = await cli.run_reindex(
        statuses=[IndexStatus.PENDING],
        limit=50,
        session_factory=session_factory,
        pipeline_factory=lambda: make_test_pipeline(session_factory, fake_embedding_provider),
    )

    # Batch semantics: failures are counted, not raised.
    assert counts == {"processed": 2, "done": 0, "failed": 2, "skipped": 0}


def test_main_dispatches_reindex_with_parsed_args(monkeypatch):
    recorded: dict[str, object] = {}

    async def fake_run_reindex(**kwargs: object) -> dict[str, int]:
        recorded.update(kwargs)
        return {"processed": 0, "done": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(cli, "run_reindex", fake_run_reindex)

    exit_code = cli.main(["reindex", "--status", "failed", "--limit", "5"])

    assert exit_code == 0
    assert recorded["statuses"] == [IndexStatus.FAILED]
    assert recorded["limit"] == 5


def test_parser_defaults_sweep_pending_and_failed():
    args = cli._build_parser().parse_args(["reindex"])

    assert args.status is None  # None means the pending+failed default set
    assert args.limit == 50


def test_parser_rejects_done_status_and_non_positive_limit():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["reindex", "--status", "done"])
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["reindex", "--limit", "0"])
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args([])


# --- worker subcommand (offline wiring smoke; no Redis connection) ---


def test_parser_accepts_worker_subcommand():
    args = cli._build_parser().parse_args(["worker"])

    assert args.command == "worker"


def test_worker_settings_wired_from_settings():
    from app.rag.worker import INDEX_DOCUMENT_TASK, build_worker_settings

    settings = hermetic_settings(REDIS_URL="redis://localhost:6379/0", INDEX_JOB_MAX_TRIES=7)
    worker_settings = build_worker_settings(settings)

    assert worker_settings.max_tries == 7
    assert worker_settings.redis_settings.host == "localhost"
    assert worker_settings.redis_settings.port == 6379
    # The registered function name is the literal the enqueuer sends
    # (cross-checked in test_arq_worker.py) — drift strands jobs.
    assert [f.name for f in worker_settings.functions] == [INDEX_DOCUMENT_TASK]
    assert worker_settings.on_startup is not None
    assert worker_settings.on_shutdown is not None


def test_main_dispatches_worker_to_run_worker(monkeypatch):
    recorded: dict[str, object] = {}

    class SentinelWorkerSettings:
        pass

    monkeypatch.setattr(
        cli, "get_settings", lambda: hermetic_settings(REDIS_URL="redis://localhost:6379/0")
    )
    monkeypatch.setattr(
        "app.rag.worker.build_worker_settings", lambda settings: SentinelWorkerSettings
    )

    def fake_run_worker(settings_cls: object, **kwargs: object) -> None:
        recorded["settings_cls"] = settings_cls

    monkeypatch.setattr("arq.run_worker", fake_run_worker)

    assert cli.main(["worker"]) == 0
    assert recorded["settings_cls"] is SentinelWorkerSettings


def test_main_refuses_worker_without_redis_url(monkeypatch):
    # Queue mode is opt-in: a worker without KB_REDIS_URL would poll a
    # default Redis that nothing enqueues to.
    monkeypatch.setattr(cli, "get_settings", lambda: hermetic_settings())

    assert cli.main(["worker"]) == 1
