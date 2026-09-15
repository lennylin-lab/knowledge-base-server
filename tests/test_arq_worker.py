"""ARQ worker: retry classification, settle semantics, poison guard.

The pipeline is real but double-backed — `FakeEmbeddingProvider` +
`RecordingEsStore` against the disposable test DB — so the worker's retry
decisions are exercised against actual `IndexStatus` transitions, not mocks
of them. `arq.worker.run_worker` itself needs Redis and lives under the
`live_redis` marker instead (test_arq_live.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import openai
import pytest
from arq import Retry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import app.rag.worker as worker_module
from app.core.exceptions import (
    LLMProviderError,
    LLMRateLimitedError,
    SearchIndexError,
)
from app.models.document import IndexStatus
from app.models.tenant import DEFAULT_TENANT_ID
from app.rag.indexer import IndexingPipeline
from app.rag.worker import (
    INDEX_DOCUMENT_TASK,
    index_document,
    is_transient_index_error,
    run_index_job,
)
from app.repositories.document import DocumentRepository
from app.schemas.document import DocumentCreate, DocumentUpdate
from app.services.document import DocumentService
from fakes import FakeEmbeddingProvider, RecordingEsStore, StubEsClient, hermetic_settings

MAX_TRIES = 3
MIN_DELAY_S = 5

SessionMaker = async_sessionmaker[AsyncSession]


def make_pipeline(session_factory: SessionMaker, provider: Any) -> IndexingPipeline:
    es_store = RecordingEsStore()
    return IndexingPipeline(
        session_factory=session_factory,
        embedding_provider=provider,
        es_client=StubEsClient(),
        es_index="kb_documents_test",
        ensure_index=es_store.ensure_index,
        replace_chunks=es_store.replace_chunks,
    )


async def seed_document(session_factory: SessionMaker, content: str = "# A\n\ntext a") -> UUID:
    async with session_factory() as session:
        created = await DocumentService(session).create_document(
            DocumentCreate(content=content), tenant_id=DEFAULT_TENANT_ID
        )
        return created.id


async def status_of(session_factory: SessionMaker, doc_id: UUID) -> IndexStatus:
    async with session_factory() as session:
        document = await DocumentRepository(session).get_by_id(doc_id, tenant_id=DEFAULT_TENANT_ID)
        assert document is not None
        return document.index_status


async def run_job(
    pipeline: IndexingPipeline,
    session_factory: SessionMaker,
    doc_id: UUID,
    *,
    job_try: int,
    expected_updated_at: datetime | None = None,
) -> None:
    """`run_index_job` against a real (double-backed) pipeline core."""
    await run_index_job(
        doc_id,
        DEFAULT_TENANT_ID,
        job_try=job_try,
        max_tries=MAX_TRIES,
        retry_min_delay_s=MIN_DELAY_S,
        expected_updated_at=expected_updated_at,
        runner=pipeline.process_document_raising,
        session_factory=session_factory,
    )


def events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == name]


# --- transient classification table (pure; design.md's unit table) ---


def _provider_status_error(
    error_type: type[openai.APIStatusError], status: int
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://provider.invalid/embeddings")
    response = httpx.Response(status, request=request)
    return error_type("provider rejected", response=response, body=None)


def _caused_by(exc: Exception, cause: BaseException) -> Exception:
    try:
        raise exc from cause
    except Exception as raised:
        return raised


def test_transient_error_classes_qualify_for_retry() -> None:
    assert is_transient_index_error(LLMRateLimitedError("slow down"))
    assert is_transient_index_error(SearchIndexError("es down"))
    assert is_transient_index_error(LLMProviderError("provider 5xx"))
    assert is_transient_index_error(ConnectionError("refused"))
    # asyncio.TimeoutError is TimeoutError, an OSError subclass since 3.11.
    assert is_transient_index_error(TimeoutError("timed out"))


def test_permanent_error_classes_settle_immediately() -> None:
    # Auth-shaped provider failures (401/403 in the SDK cause chain).
    assert not is_transient_index_error(
        _caused_by(
            LLMProviderError("bad key"), _provider_status_error(openai.AuthenticationError, 401)
        )
    )
    assert not is_transient_index_error(
        _caused_by(
            LLMProviderError("no permission"),
            _provider_status_error(openai.PermissionDeniedError, 403),
        )
    )
    # Arbitrary bugs: a retry cannot fix code.
    assert not is_transient_index_error(KeyError("bug"))
    assert not is_transient_index_error(ValueError("bad width"))


# --- run_index_job: retry vs settle vs skip vs done (real pipeline doubles) ---


@pytest.mark.db
async def test_transient_error_before_final_try_raises_retry_and_leaves_pending(
    session_factory: SessionMaker, fake_embedding_provider: FakeEmbeddingProvider
) -> None:
    doc_id = await seed_document(session_factory)
    fake_embedding_provider.error = LLMRateLimitedError("slow down")
    pipeline = make_pipeline(session_factory, fake_embedding_provider)

    with capture_logs() as logs, pytest.raises(Retry) as retry_info:
        await run_job(pipeline, session_factory, doc_id, job_try=1)

    assert retry_info.value.defer_score == 5_000  # first backoff step: 5s
    # Not settled: a retry is still pending, so the status stays pending.
    assert await status_of(session_factory, doc_id) is IndexStatus.PENDING
    retries = events(logs, "index_job_retry")
    assert len(retries) == 1
    assert retries[0]["log_level"] == "warning"
    assert retries[0]["error_class"] == "LLMRateLimitedError"
    assert retries[0]["next_delay_s"] == 5
    assert "slow down" not in str(retries)  # no error content in logs


@pytest.mark.db
async def test_transient_error_backoff_doubles_per_attempt(
    session_factory: SessionMaker, fake_embedding_provider: FakeEmbeddingProvider
) -> None:
    doc_id = await seed_document(session_factory)
    fake_embedding_provider.error = LLMProviderError("provider down")
    pipeline = make_pipeline(session_factory, fake_embedding_provider)

    with pytest.raises(Retry) as second_try:
        await run_job(pipeline, session_factory, doc_id, job_try=2)

    assert second_try.value.defer_score == 10_000  # 5s * 2**(2-1)


@pytest.mark.db
async def test_transient_error_on_final_try_settles_failed_without_raising(
    session_factory: SessionMaker, fake_embedding_provider: FakeEmbeddingProvider
) -> None:
    doc_id = await seed_document(session_factory)
    fake_embedding_provider.error = SearchIndexError("es down")
    pipeline = make_pipeline(session_factory, fake_embedding_provider)

    with capture_logs() as logs:
        await run_job(pipeline, session_factory, doc_id, job_try=MAX_TRIES)

    assert await status_of(session_factory, doc_id) is IndexStatus.FAILED
    assert events(logs, "index_job_retry") == []  # no retry requested
    finished = events(logs, "index_job_finished")
    assert len(finished) == 1
    assert finished[0]["outcome"] == "failed"
    assert finished[0]["error_class"] == "SearchIndexError"


@pytest.mark.db
async def test_permanent_error_settles_failed_on_first_attempt(
    session_factory: SessionMaker, fake_embedding_provider: FakeEmbeddingProvider
) -> None:
    doc_id = await seed_document(session_factory)
    fake_embedding_provider.error = _caused_by(
        LLMProviderError("bad key"), _provider_status_error(openai.AuthenticationError, 401)
    )
    pipeline = make_pipeline(session_factory, fake_embedding_provider)

    with capture_logs() as logs:
        await run_job(pipeline, session_factory, doc_id, job_try=1)

    assert await status_of(session_factory, doc_id) is IndexStatus.FAILED
    assert events(logs, "index_job_retry") == []  # no retry burn on 401-shaped failures


@pytest.mark.db
async def test_missing_document_is_a_clean_skip(
    session_factory: SessionMaker, fake_embedding_provider: FakeEmbeddingProvider
) -> None:
    pipeline = make_pipeline(session_factory, fake_embedding_provider)

    with capture_logs() as logs:
        await run_job(pipeline, session_factory, uuid4(), job_try=1)

    assert events(logs, "index_job_retry") == []
    finished = events(logs, "index_job_finished")
    assert len(finished) == 1
    assert finished[0]["outcome"] == "skipped"


@pytest.mark.db
async def test_successful_run_marks_document_done(
    session_factory: SessionMaker, fake_embedding_provider: FakeEmbeddingProvider
) -> None:
    doc_id = await seed_document(session_factory)
    pipeline = make_pipeline(session_factory, fake_embedding_provider)

    with capture_logs() as logs:
        await run_job(pipeline, session_factory, doc_id, job_try=1)

    assert await status_of(session_factory, doc_id) is IndexStatus.DONE
    finished = events(logs, "index_job_finished")
    assert len(finished) == 1
    assert finished[0]["outcome"] == "done"
    started = events(logs, "index_job_started")
    assert len(started) == 1 and started[0]["job_try"] == 1


@pytest.mark.db
async def test_retry_after_newer_edit_skips_instead_of_reindexing(
    session_factory: SessionMaker, fake_embedding_provider: FakeEmbeddingProvider
) -> None:
    """The guard rides along on retries: an attempt whose document has since
    been edited skips (the newer save's job owns it) instead of re-indexing
    an obsolete version."""
    async with session_factory() as session:
        created = await DocumentService(session).create_document(
            DocumentCreate(content="# A\n\ntext a"), tenant_id=DEFAULT_TENANT_ID
        )
    doc_id, enqueued_version = created.id, created.updated_at

    fake_embedding_provider.error = LLMRateLimitedError("slow down")
    pipeline = make_pipeline(session_factory, fake_embedding_provider)
    with pytest.raises(Retry):
        await run_job(
            pipeline, session_factory, doc_id, job_try=1, expected_updated_at=enqueued_version
        )

    # The document is edited while the retry waits — the queued version is stale.
    async with session_factory() as session:
        await DocumentService(session).update_document(
            doc_id, DocumentUpdate(title="edited"), tenant_id=DEFAULT_TENANT_ID
        )
    fake_embedding_provider.error = None
    fake_embedding_provider.calls.clear()  # attempt 1 errored mid-embed already

    with capture_logs() as logs:
        await run_job(
            pipeline, session_factory, doc_id, job_try=2, expected_updated_at=enqueued_version
        )

    assert fake_embedding_provider.calls == []  # zero embed work on the retry
    assert await status_of(session_factory, doc_id) is IndexStatus.PENDING
    assert events(logs, "index_job_retry") == []  # a skip is not a retry-worthy failure
    skipped = events(logs, "index_job_skipped_stale")
    assert len(skipped) == 1
    assert skipped[0]["document_id"] == str(doc_id)
    finished = events(logs, "index_job_finished")
    assert len(finished) == 1
    assert finished[0]["outcome"] == "skipped"


# --- index_document: the thin arq adapter (str payload, ctx job_try) ---


async def test_malformed_doc_id_is_skipped_as_poison() -> None:
    with capture_logs() as logs:
        await index_document({"job_try": 1}, "not-a-uuid", str(DEFAULT_TENANT_ID))

    poison = events(logs, "index_job_poison")
    assert len(poison) == 1
    assert poison[0]["document_id"] == "not-a-uuid"
    assert events(logs, "index_job_retry") == []


@pytest.mark.db
async def test_index_document_reads_job_try_from_ctx_and_module_seams(
    session_factory: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await seed_document(session_factory)
    seen_versions: list[datetime | None] = []

    async def flaky_runner(
        doc_id: UUID, tenant_id: UUID, expected_updated_at: datetime | None
    ) -> IndexStatus | None:
        assert tenant_id == DEFAULT_TENANT_ID
        seen_versions.append(expected_updated_at)
        raise LLMProviderError("provider down")

    monkeypatch.setattr(worker_module, "run_indexing_raw", flaky_runner)
    monkeypatch.setattr(worker_module, "SessionFactory", session_factory)
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: hermetic_settings(
            INDEX_JOB_MAX_TRIES=MAX_TRIES, INDEX_JOB_RETRY_MIN_DELAY_S=MIN_DELAY_S
        ),
    )

    # Absent job_try defaults to 1 -> transient -> retry requested.
    with pytest.raises(Retry):
        await index_document({}, str(doc_id), str(DEFAULT_TENANT_ID))
    assert await status_of(session_factory, doc_id) is IndexStatus.PENDING

    # ctx job_try at the budget's end -> settle without raising.
    await index_document({"job_try": MAX_TRIES}, str(doc_id), str(DEFAULT_TENANT_ID))
    assert await status_of(session_factory, doc_id) is IndexStatus.FAILED

    # Legacy payloads (no timestamp) degrade to a guard-free run.
    assert seen_versions == [None, None]


@pytest.mark.db
@pytest.mark.parametrize(
    "bad_stamp",
    [
        "not-a-timestamp",  # unparseable string
        "2026-09-04T12:00:00",  # timezone-naive ISO: could never match PG's aware value
        123,  # non-string (JSON payloads can carry any type)
    ],
)
async def test_unusable_timestamp_payload_degrades_to_no_guard(
    session_factory: SessionMaker, monkeypatch: pytest.MonkeyPatch, bad_stamp: object
) -> None:
    """A timestamp that cannot act as a version disables the guard — the job
    still runs (never crashes on a bad payload). Naive stamps in particular
    must NOT silently skip every job: PG emits tz-aware values, so a naive
    one can never match and degrading is the honest reading."""
    doc_id = await seed_document(session_factory)
    pipeline = make_pipeline(session_factory, FakeEmbeddingProvider())
    monkeypatch.setattr(worker_module, "run_indexing_raw", pipeline.process_document_raising)
    monkeypatch.setattr(worker_module, "SessionFactory", session_factory)
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: hermetic_settings(
            INDEX_JOB_MAX_TRIES=MAX_TRIES, INDEX_JOB_RETRY_MIN_DELAY_S=MIN_DELAY_S
        ),
    )

    # The non-string case deliberately violates the typed signature: JSON
    # payloads are untyped at runtime and the worker must survive that.
    with capture_logs() as logs:
        await index_document({"job_try": 1}, str(doc_id), str(DEFAULT_TENANT_ID), bad_stamp)

    assert await status_of(session_factory, doc_id) is IndexStatus.DONE
    degraded = events(logs, "index_job_invalid_expected_updated_at")
    assert len(degraded) == 1
    assert degraded[0]["log_level"] == "warning"
    assert degraded[0]["document_id"] == str(doc_id)
    finished = events(logs, "index_job_finished")
    assert len(finished) == 1
    assert finished[0]["outcome"] == "done"


def test_index_document_task_name_is_stable() -> None:
    # The enqueuer (api/deps.py) sends this literal string; a rename here
    # would silently strand jobs. Cross-checked against the worker settings
    # registration in test_cli.py.
    assert INDEX_DOCUMENT_TASK == "index_document"
    assert index_document.__qualname__ == "index_document"
