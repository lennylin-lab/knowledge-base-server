"""Summarize orchestration: document load, chunk-aware passes, lifecycle logs.

Synchronous single response (unlike chat's SSE stream): one call yields one
computed summary, never persisted. Long documents summarize map-reduce style
with the production chunker — one model pass per chunk, sequentially (no
parallel fan-out in v1), then one combine pass over the chunk summaries.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import openai
import structlog
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.summarize import (
    SummarizeDeps,
    build_summarize_agent,
    render_document_prompt,
    render_reduce_prompt,
)
from app.core.exceptions import (
    AppError,
    LLMProviderError,
    LLMRateLimitedError,
    NotFoundError,
)
from app.models.document import Document
from app.rag.chunker import chunk_markdown
from app.repositories.document import DocumentRepository
from app.schemas.agents import SummaryResult

logger = structlog.get_logger(__name__)


class SummarizeService:
    """Computes (never persists) one document summary per call."""

    def __init__(
        self,
        model: Model,
        model_name: str,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._model_name = model_name
        self._session_factory = session_factory
        # Built once per process (the service itself is process-lifetime);
        # per-request state rides in SummarizeDeps, never on the agent.
        self._agent = build_summarize_agent(model)

    async def summarize_document(self, doc_id: UUID) -> SummaryResult:
        """Summarize one live document; missing/soft-deleted raise NotFoundError.

        Content that fits a single chunk is summarized in one pass; longer
        content is chunked (map) and combined (reduce). Provider failures
        surface as their `AppError` taxonomy so the shared handler returns
        the matching envelope. Document content and summary text are never
        logged — ids, lengths, and token counts only.
        """
        started = time.perf_counter()
        # The load happens before anything else: a missing document must 404
        # without a model call, and the run log needs the id bound.
        document = await self._load_document(doc_id)
        log = logger.bind(document_id=str(doc_id), run_id=uuid4().hex)
        deps = SummarizeDeps(title=document.title, tags=list(document.tags))
        # Front-matter-only (or whitespace-only) bodies chunk to nothing; the
        # degenerate path summarizes the RAW stored content in one pass. Raw is
        # deliberate: the stripped body would be empty, and the title/tags
        # inside the YAML only duplicate the header the prompt already renders.
        chunks = chunk_markdown(document.content) or [document.content]

        log.info("agent_run_started", agent="summarize", content_length=len(document.content))

        input_tokens = 0
        output_tokens = 0
        try:
            if len(chunks) == 1:
                summary, tokens_in, tokens_out = await self._run_pass(
                    deps, render_document_prompt(deps, chunks[0])
                )
                runs = 1
            else:
                section_summaries: list[str] = []
                for index, chunk in enumerate(chunks, start=1):
                    text, tokens_in, tokens_out = await self._run_pass(
                        deps,
                        render_document_prompt(deps, chunk, section=(index, len(chunks))),
                    )
                    section_summaries.append(text)
                    input_tokens += tokens_in
                    output_tokens += tokens_out
                summary, tokens_in, tokens_out = await self._run_pass(
                    deps, render_reduce_prompt(deps, section_summaries)
                )
                runs = len(chunks) + 1
            input_tokens += tokens_in
            output_tokens += tokens_out
        except Exception as exc:
            failure = _as_app_error(exc)
            # Run-level audit event, not a boundary log: it closes the
            # agent_run_started trail with run-scoped context (run_id, latency,
            # error_class, traceback) that the request-boundary `app_error`
            # handler line cannot carry. Re-raising hands the envelope to that
            # handler — two events, two scopes, no duplicated payload.
            log.exception(
                "agent_run_failed",
                agent="summarize",
                outcome=failure.code,
                error_class=type(exc).__name__,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise failure from exc

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.info(
            "agent_run_finished",
            agent="summarize",
            model=self._model_name,
            outcome="success",
            runs=runs,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return SummaryResult(
            document_id=doc_id,
            summary=summary,
            model=self._model_name,
            latency_ms=latency_ms,
        )

    async def _load_document(self, doc_id: UUID) -> Document:
        """Fetch one live document; soft-deleted counts as missing."""
        async with self._session_factory() as session:
            document = await DocumentRepository(session).get_by_id(doc_id)
        if document is None:
            raise NotFoundError(f"Document {doc_id} not found")
        return document

    async def _run_pass(self, deps: SummarizeDeps, prompt: str) -> tuple[str, int, int]:
        """One non-streaming model pass: (summary text, input tokens, output tokens)."""
        result = await self._agent.run(prompt, deps=deps)
        usage = result.usage
        return result.output, usage.input_tokens or 0, usage.output_tokens or 0


def _as_app_error(exc: Exception) -> AppError:
    """Map a failed run onto the error taxonomy — same mapping as chat's.

    Chat converts the mapping into a terminal SSE event because its stream is
    already open; this endpoint re-raises instead so the shared handler
    returns the matching HTTP envelope. Details stay in logs either way.
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitedError("LLM provider rate limit exceeded")
    if isinstance(exc, openai.APIError):
        return LLMProviderError("LLM provider request failed")
    return AppError("Internal server error")
