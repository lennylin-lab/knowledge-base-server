"""Agent orchestration: summarize passes, association candidates, writing runs.

Summarize and association are synchronous single responses: one call yields
one computed result, never persisted. Summarize runs chunk-aware passes —
long documents map-reduce style with the production chunker (one model pass
per chunk, sequentially, then one combine pass). Association gathers its
deterministic candidates (pgvector neighbors + tag overlap) BEFORE any model
call, so the LLM only curates what the database surfaced. Writing streams
suggestions over chat's SSE event vocabulary, with `ChatService.ask`'s stream
discipline: run_id binding, sources flushed per tool call, and nothing may
escape `suggest` once the first event is yielded.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from uuid import UUID, uuid4

import openai
import structlog
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.association import (
    AssociationCandidate,
    AssociationDeps,
    AssociationsOutput,
    build_association_agent,
    render_association_prompt,
)
from app.agents.qa import SourceCollector
from app.agents.summarize import (
    SummarizeDeps,
    build_summarize_agent,
    render_document_prompt,
    render_reduce_prompt,
)
from app.agents.writing import WritingDeps, build_writing_agent, render_writing_prompt
from app.core.exceptions import (
    AppError,
    LLMProviderError,
    LLMRateLimitedError,
    NotFoundError,
)
from app.models.document import Document
from app.rag.chunker import chunk_markdown
from app.rag.retriever import Retriever
from app.repositories.document import DocumentRepository, TagOverlapRow
from app.repositories.document_chunk import DocumentChunkRepository, NeighborDocumentRow
from app.schemas.agents import AssociationItem, AssociationsResult, SummaryResult
from app.schemas.chat import (
    AnswerDeltaEvent,
    ChatStreamEvent,
    DoneEvent,
    ErrorEvent,
    RunStartedEvent,
    SearchMode,
    SourcesEvent,
)
from app.schemas.search import SearchHit

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
    already open; sync endpoints re-raise instead so the shared handler
    returns the matching HTTP envelope. Details stay in logs either way.
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitedError("LLM provider rate limit exceeded")
    if isinstance(exc, openai.APIError):
        return LLMProviderError("LLM provider request failed")
    return AppError("Internal server error")


# Both candidate legs are bounded to this size (PRD: ~10 per leg) — enough
# signal for the model without an unbounded prompt.
CANDIDATE_LIMIT = 10
# Fallback excerpt bound when the source has no chunks yet: matches the
# chunker's per-chunk ceiling, so indexed and unindexed sources feed the
# prompt similarly sized excerpts.
EXCERPT_CHAR_LIMIT = 1600


class AssociationService:
    """Computes (never persists) one document's related documents per call."""

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
        # per-request state rides in AssociationDeps, never on the agent.
        self._agent = build_association_agent(model)

    async def associate_document(self, doc_id: UUID) -> AssociationsResult:
        """Curate related documents for one live document via the LLM.

        Deterministic candidates are gathered before any model call: a
        missing/soft-deleted source raises NotFoundError and a source with no
        candidates at all returns an empty result — neither ever reaches the
        model. The model's selection is joined back onto the candidate
        metadata; ids it returned that were not candidates (hallucinated or
        duplicated) are dropped, so the response only ever carries
        deterministic metadata plus the LLM-written reasons. Reasons and
        content are never logged — ids and counts only.
        """
        started = time.perf_counter()
        document, candidates, excerpt = await self._gather(doc_id)
        log = logger.bind(document_id=str(doc_id), run_id=uuid4().hex)
        if not candidates:
            # No signal to curate: skip the run (and its lifecycle events)
            # rather than logging a run that never happened.
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            log.info(
                "agent_run_skipped",
                agent="association",
                reason="no_candidates",
                latency_ms=latency_ms,
            )
            return AssociationsResult(
                document_id=doc_id,
                associations=[],
                model=self._model_name,
                latency_ms=latency_ms,
            )

        deps = AssociationDeps(title=document.title, tags=list(document.tags))
        prompt = render_association_prompt(deps, excerpt, candidates)
        log.info("agent_run_started", agent="association", candidate_count=len(candidates))
        try:
            result = await self._agent.run(prompt, deps=deps)
        except Exception as exc:
            failure = _as_app_error(exc)
            # Run-level audit event closing the agent_run_started trail (see
            # the summarize twin for the two-scopes rationale); the envelope
            # comes from re-raising into the shared handler.
            log.exception(
                "agent_run_failed",
                agent="association",
                outcome=failure.code,
                error_class=type(exc).__name__,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise failure from exc

        items, dropped = _join_selections(result.output, candidates)
        usage = result.usage
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.info(
            "agent_run_finished",
            agent="association",
            model=self._model_name,
            outcome="success",
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            candidate_count=len(candidates),
            selected_count=len(items),
            dropped_count=dropped,
        )
        return AssociationsResult(
            document_id=doc_id,
            associations=items,
            model=self._model_name,
            latency_ms=latency_ms,
        )

    async def _gather(self, doc_id: UUID) -> tuple[Document, list[AssociationCandidate], str]:
        """Load the live source, both candidate legs, and a bounded excerpt.

        One session for the whole read set. The document load comes first: a
        missing or soft-deleted source must 404 before any other work. The
        vector leg needs the source's own chunks — no chunks means it returns
        nothing and the run continues on tag candidates alone.
        """
        async with self._session_factory() as session:
            documents = DocumentRepository(session)
            chunks = DocumentChunkRepository(session)
            document = await documents.get_by_id(doc_id)
            if document is None:
                raise NotFoundError(f"Document {doc_id} not found")
            vector_rows = await chunks.find_neighbor_documents(doc_id, limit=CANDIDATE_LIMIT)
            tag_rows = await documents.find_by_tag_overlap(
                document.tags, exclude_id=doc_id, limit=CANDIDATE_LIMIT
            )
            excerpt = await chunks.first_chunk_content(doc_id)
        if excerpt is None:
            excerpt = document.content[:EXCERPT_CHAR_LIMIT]
        return document, _merge_candidates(vector_rows, tag_rows), excerpt


def _vector_signal(distance: float) -> str:
    """Human-readable vector-leg signal for prompts and responses."""
    return f"similar content (cosine distance {distance:.4f})"


def _tag_signal(shared_tags: Sequence[str]) -> str:
    """Human-readable tag-leg signal for prompts and responses."""
    return f"shared tags: {', '.join(shared_tags)}"


def _merge_candidates(
    vector_rows: Sequence[NeighborDocumentRow], tag_rows: Sequence[TagOverlapRow]
) -> list[AssociationCandidate]:
    """Union both legs into one candidate list, vector-ranked first.

    A document surfaced by both legs appears once with both signals; a
    document only the tag leg found keeps its own signal. Leg order is
    deterministic (distance then id; recency then id), so the prompt order is
    too.
    """
    tag_shared = {tag_row.document_id: tag_row.shared_tags for tag_row in tag_rows}
    candidates: dict[UUID, AssociationCandidate] = {}
    for vector_row in vector_rows:
        shared = tag_shared.get(vector_row.document_id)
        signal = _vector_signal(vector_row.distance)
        if shared:
            signal = f"{signal}; {_tag_signal(shared)}"
        candidates[vector_row.document_id] = AssociationCandidate(
            document_id=vector_row.document_id,
            title=vector_row.title,
            tags=list(vector_row.tags),
            signal=signal,
        )
    for tag_row in tag_rows:
        if tag_row.document_id in candidates:
            continue
        candidates[tag_row.document_id] = AssociationCandidate(
            document_id=tag_row.document_id,
            title=tag_row.title,
            tags=list(tag_row.tags),
            signal=_tag_signal(tag_row.shared_tags),
        )
    return list(candidates.values())


def _join_selections(
    output: AssociationsOutput, candidates: Sequence[AssociationCandidate]
) -> tuple[list[AssociationItem], int]:
    """Join the LLM's picks back onto candidate metadata.

    Every returned item carries the deterministic title/tags/signal gathered
    before the run — the model contributes only the selection and the reason.
    Picks whose id was not a candidate, or repeats an already-joined one, are
    dropped; the count (never the content) is returned for logging.
    """
    by_id = {candidate.document_id: candidate for candidate in candidates}
    items: list[AssociationItem] = []
    seen: set[UUID] = set()
    dropped = 0
    for pick in output.associations:
        candidate = by_id.get(pick.document_id)
        if candidate is None or pick.document_id in seen:
            dropped += 1
            continue
        seen.add(pick.document_id)
        items.append(
            AssociationItem(
                document_id=candidate.document_id,
                title=candidate.title,
                tags=list(candidate.tags),
                reason=pick.reason,
                signal=candidate.signal,
            )
        )
    return items, dropped


def _drain(pending: list[list[SearchHit]]) -> list[list[SearchHit]]:
    """Take all pending retrieval batches, leaving the buffer empty.

    Twin of `services.chat._drain`, kept local rather than imported across
    service modules' private names: four trivial lines, and chat's module
    stays untouched.
    """
    batches = list(pending)
    pending.clear()
    return batches


class WritingService:
    """Streams one writing-suggestion run per draft (chat's event vocabulary).

    Mirrors `ChatService.ask`'s discipline: run_id bound into structlog
    contextvars for the whole generator, sources flushed as soon as each
    retrieval tool call returns, and every failure after the first event
    becoming a terminal `error` event — nothing may escape `suggest`.
    Retrieval is OPTIONAL (the model decides), so a zero-tool run simply has
    no `sources` event and reports `tool_calls=0`. Draft and instruction text
    are never logged; lengths only.
    """

    def __init__(
        self,
        retriever: Retriever,
        model: Model,
        *,
        mode: SearchMode,
        extra_tools: Sequence[Tool[WritingDeps]] = (),
    ) -> None:
        self._retriever = retriever
        self._mode = mode
        self._model = model
        # Built once per process (the service itself is process-lifetime);
        # per-request state rides in WritingDeps, never on the agent. The
        # extra_tools seam stays open for wrapped MCP tools in a later task.
        self._agent = build_writing_agent(model, extra_tools=extra_tools)

    async def suggest(
        self, draft: str, instruction: str | None = None, *, limit: int = 8
    ) -> AsyncIterator[ChatStreamEvent]:
        """Run one writing-assistance turn, yielding chat-contract events.

        The draft (plus optional instruction) is rendered into the user
        prompt; the model then streams its suggestion, calling
        `search_knowledge` only when it judges the knowledge base helpful.
        """
        run_id = uuid4().hex
        structlog.contextvars.bind_contextvars(run_id=run_id)
        started = time.perf_counter()

        # The service owns the flush hook: the agent appends, the service
        # drains (same hand-off as chat, so `agents/` stays stream-free).
        pending: list[list[SearchHit]] = []
        collector = SourceCollector(on_append=pending.append)
        deps = WritingDeps(retriever=self._retriever, limit=limit, collector=collector)

        logger.info(
            "agent_run_started",
            agent="writing",
            draft_length=len(draft),
            instruction_length=len(instruction) if instruction is not None else None,
        )
        yield RunStartedEvent(run_id=run_id, mode=self._mode)

        usage_input_tokens: int | None = None
        usage_output_tokens: int | None = None
        try:
            async with self._agent.run_stream(
                render_writing_prompt(draft, instruction), deps=deps
            ) as result:
                async for delta in result.stream_text(delta=True, debounce_by=None):
                    # Tool calls (and their sources) can land between parts;
                    # drain before the part so sources always precede the text
                    # they ground.
                    for batch in _drain(pending):
                        yield SourcesEvent(items=batch)
                    if delta:
                        yield AnswerDeltaEvent(text=delta)
                usage = result.usage
                usage_input_tokens = usage.input_tokens or None
                usage_output_tokens = usage.output_tokens or None
            for batch in _drain(pending):
                yield SourcesEvent(items=batch)
        except Exception as exc:
            failure = _as_app_error(exc)
            logger.exception(
                "agent_run_failed",
                agent="writing",
                outcome=failure.code,
                error_class=type(exc).__name__,
                tool_calls=collector.tool_calls,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            yield ErrorEvent(code=failure.code, message=failure.message)
            return

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "agent_run_finished",
            agent="writing",
            model=self._model.model_name,
            outcome="success",
            tool_calls=collector.tool_calls,
            latency_ms=latency_ms,
            input_tokens=usage_input_tokens,
            output_tokens=usage_output_tokens,
        )
        yield DoneEvent(
            run_id=run_id,
            outcome="success",
            tool_calls=collector.tool_calls,
            latency_ms=latency_ms,
        )
