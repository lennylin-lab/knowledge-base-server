"""ARQ worker: queue-level retries for the indexing pipeline.

arq 0.28 task contract (verified against the installed source): a plain
exception FAILS the job — a retry must be requested explicitly by raising
`arq.worker.Retry`. `index_document` therefore classifies pipeline failures
itself:

- transient failure, attempts left  -> raise `Retry` (arq re-runs the job
  after the backoff computed from `INDEX_JOB_RETRY_MIN_DELAY_S`)
- transient failure, final attempt  -> settle `index_status=failed`, complete
- permanent failure                 -> settle `failed` immediately, no retry burn
- missing/soft-deleted document     -> clean skip (`None` from the pipeline)

The worker process never imports the FastAPI app factory; each job builds
and closes its own pipeline clients (the `run_indexing` pattern).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar
from uuid import UUID

import structlog
from arq import Retry, func
from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerSettingsType
from arq.worker import Function
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import SessionFactory
from app.core.exceptions import LLMProviderError, LLMRateLimitedError, SearchIndexError
from app.core.logging import configure_logging
from app.llm.errors import is_permanent_provider_error
from app.models.document import IndexStatus
from app.rag.indexer import run_indexing_raw
from app.repositories.document import DocumentRepository

logger = structlog.get_logger(__name__)

# Queue name the web process enqueues indexing jobs under; the registration
# in `WorkerSettings` uses the same constant, so the two ends cannot drift.
INDEX_DOCUMENT_TASK = "index_document"

# The raising pipeline core (`None` = missing-document skip).
type IndexRunner = Callable[[UUID], Awaitable[IndexStatus | None]]


def is_transient_index_error(exc: BaseException) -> bool:
    """True when a retry has a realistic chance of helping.

    Rate limits, provider outages, search-index failures and network-level
    errors qualify (`ConnectionError` and `TimeoutError` — asyncio timeouts
    included — are `OSError` subclasses). Auth-shaped provider errors and
    arbitrary bugs (`KeyError`, `ValueError`, ...) do not: retrying cannot
    fix credentials or code, so they settle `failed` immediately instead of
    burning the attempt budget.
    """
    if isinstance(exc, (LLMRateLimitedError, SearchIndexError, OSError)):
        return True
    return isinstance(exc, LLMProviderError) and not is_permanent_provider_error(exc)


async def index_document(ctx: dict[str, Any], doc_id: str) -> None:
    """ARQ task: index one document (`doc_id` is a str — JSON-safe payload)."""
    try:
        parsed_id = UUID(doc_id)
    except ValueError:
        # Poison-message guard: a malformed id can never succeed and a retry
        # cannot fix the payload — log and complete.
        logger.warning("index_job_poison", document_id=doc_id)
        return
    settings = get_settings()
    await run_index_job(
        parsed_id,
        # arq sets `job_try` on the ctx (1-based attempt counter); 1 is the
        # sensible floor if a caller ever runs the task outside a worker.
        job_try=int(ctx.get("job_try", 1)),
        max_tries=settings.INDEX_JOB_MAX_TRIES,
        retry_min_delay_s=settings.INDEX_JOB_RETRY_MIN_DELAY_S,
        runner=run_indexing_raw,
        session_factory=SessionFactory,
    )


async def run_index_job(
    doc_id: UUID,
    *,
    job_try: int,
    max_tries: int,
    retry_min_delay_s: int,
    runner: IndexRunner,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Retry-deciding core of the task (`index_document` is the thin adapter).

    arq only ever runs a job with `job_try <= max_tries` — beyond that the
    pickup itself fails the job — so the final in-function attempt must
    settle the document here rather than raise one more `Retry`.
    """
    started = time.perf_counter()
    log = logger.bind(document_id=str(doc_id), job_try=job_try)
    log.info("index_job_started")
    try:
        outcome = await runner(doc_id)
    except Exception as exc:
        error_class = type(exc).__name__
        if is_transient_index_error(exc) and job_try < max_tries:
            next_delay_s = retry_min_delay_s * 2 ** (job_try - 1)
            log.warning("index_job_retry", error_class=error_class, next_delay_s=next_delay_s)
            raise Retry(defer=next_delay_s) from exc
        # Final attempt (budget exhausted) or a permanent failure: settle so
        # users and the CLI sweep see the document's true state.
        await _set_index_status(session_factory, doc_id, IndexStatus.FAILED)
        log.info(
            "index_job_finished",
            outcome=IndexStatus.FAILED.value,
            error_class=error_class,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return
    log.info(
        "index_job_finished",
        outcome="skipped" if outcome is None else outcome.value,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def _set_index_status(
    session_factory: async_sessionmaker[AsyncSession], doc_id: UUID, status: IndexStatus
) -> None:
    """Flip `index_status` in a small follow-up transaction; never raise.

    If the DB itself is unhappy the document keeps its previous status and
    stays retriable via the CLI sweep — the same contract as the pipeline's
    `_mark_failed`.
    """
    try:
        async with session_factory() as session:
            await DocumentRepository(session).set_index_status(doc_id, status)
            await session.commit()
    except Exception:
        logger.exception("index_status_update_failed", document_id=str(doc_id))


async def worker_on_startup(ctx: dict[str, Any]) -> None:
    """Worker boot: structured logging from Settings (same config as the app)."""
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    logger.info("index_worker_started", task=INDEX_DOCUMENT_TASK)


async def worker_on_shutdown(ctx: dict[str, Any]) -> None:
    """Worker stop. No client of ours outlives a job: each run builds and
    closes its own pipeline (see `run_indexing_raw`), and arq closes the
    Redis pool itself."""
    logger.info("index_worker_stopped")


class WorkerSettings:
    """ARQ worker settings (the arq convention: `run_worker` reads these as
    class attributes).

    Only the environment-independent parts live here — the Redis DSN and
    retry budget come from Settings, so callers hand the class built by
    `build_worker_settings` to `arq.worker.run_worker`. arq's `get_kwargs`
    inspects a settings class's OWN `__dict__` (no MRO walk), which is why
    the factory re-declares every field on the returned subclass.
    """

    functions: ClassVar[Sequence[Function]] = [func(index_document, name=INDEX_DOCUMENT_TASK)]
    # No cron jobs today (scheduled indexing is out of scope).
    cron_jobs: ClassVar[Sequence[CronJob] | None] = None
    on_startup: ClassVar[StartupShutdown] = worker_on_startup
    on_shutdown: ClassVar[StartupShutdown] = worker_on_shutdown


def build_worker_settings(settings: Settings) -> WorkerSettingsType:
    """The configured worker settings class for `arq.worker.run_worker`.

    The return type is arq's own alias ("whatever `run_worker` accepts");
    the returned class carries every field in its own `__dict__`, per the
    `get_kwargs` note on `WorkerSettings`.
    """
    return type(
        "ConfiguredWorkerSettings",
        (WorkerSettings,),
        {
            "functions": list(WorkerSettings.functions),
            "cron_jobs": WorkerSettings.cron_jobs,
            "on_startup": WorkerSettings.on_startup,
            "on_shutdown": WorkerSettings.on_shutdown,
            "redis_settings": RedisSettings.from_dsn(settings.REDIS_URL),
            "max_tries": settings.INDEX_JOB_MAX_TRIES,
        },
    )
