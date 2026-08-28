"""Operations CLI: `python -m app.cli reindex`.

Compensation path for the background indexing pipeline — sweeps documents
still `pending`/`failed` (retry-after-failure and backfill). Per-document
failures do not fail the batch: the summary carries the counts.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.database import SessionFactory
from app.core.logging import configure_logging
from app.models.document import IndexStatus
from app.rag.indexer import IndexingPipeline, build_default_pipeline
from app.repositories.document import DocumentRepository

logger = structlog.get_logger(__name__)

PipelineFactory = Callable[[], IndexingPipeline]


def _build_parser() -> argparse.ArgumentParser:
    """argparse setup (stdlib only — no new dependency for a CLI)."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Knowledge base operations CLI.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    reindex = subcommands.add_parser(
        "reindex",
        help="Re-index documents whose index_status is pending or failed.",
    )
    reindex.add_argument(
        "--status",
        action="append",
        choices=[IndexStatus.PENDING.value, IndexStatus.FAILED.value],
        help="Restrict the sweep (repeatable); default: pending + failed. "
        "Re-indexing done documents (full rebuild) is deliberately not offered.",
    )
    reindex.add_argument(
        "--limit",
        type=_positive_int,
        default=50,
        help="Bound one run to N documents (default: 50).",
    )
    return parser


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


async def run_reindex(
    *,
    statuses: Sequence[IndexStatus],
    limit: int,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    pipeline_factory: PipelineFactory = build_default_pipeline,
) -> dict[str, int]:
    """Process up to `limit` documents in the given statuses; return counts.

    Exit-worthy failures (e.g. the database being down) propagate; per-document
    indexing failures are already swallowed by the pipeline (status=failed).
    """
    pipeline = pipeline_factory()
    try:
        async with session_factory() as session:
            documents = await DocumentRepository(session).list_by_index_status(
                statuses, limit=limit
            )
            document_ids = [document.id for document in documents]

        counts = {"processed": 0, "done": 0, "failed": 0, "skipped": 0}
        for doc_id in document_ids:
            outcome = await pipeline.process_document(doc_id)
            counts["processed"] += 1
            if outcome is None:
                counts["skipped"] += 1
            else:
                counts[outcome.value] += 1
    finally:
        await pipeline.aclose()

    logger.info(
        "reindex_batch",
        processed=counts["processed"],
        done=counts["done"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        statuses=[status.value for status in statuses],
        limit=limit,
    )
    return counts


# Default sweep set; `done` is intentionally excluded (no full rebuild).
_DEFAULT_STATUSES: list[str] = [IndexStatus.PENDING.value, IndexStatus.FAILED.value]


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns 0 even with per-document failures (batch semantics)."""
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    args = _build_parser().parse_args(argv)

    if args.command == "reindex":
        statuses = [IndexStatus(value) for value in (args.status or _DEFAULT_STATUSES)]
        asyncio.run(run_reindex(statuses=statuses, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
