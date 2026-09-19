"""Agent-operation business logic: drafts, resume, and the atomic apply.

The service owns every transition of `OperationState` and the single
transaction that publishes a draft: version check -> document write ->
revision row -> `applied`, committed once, indexing enqueued only after
commit. Stale versions and duplicate applies never mutate anything (the
duplicate reads back the original revision instead of writing a second one).
Drafts live only on `agent_operations` — nothing here touches
`chat_messages`, so agent work can never leak into chat history.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import structlog
from pydantic_ai import Agent
from pydantic_ai.messages import (
    AgentStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_ai.models import Model
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.qa import SourceCollector
from app.agents.writing import (
    DraftOutput,
    WritingDeps,
    build_draft_agent,
    render_writing_prompt,
)
from app.core.exceptions import (
    AppError,
    ChatUnavailableError,
    ConflictError,
    NotFoundError,
)
from app.models.document import Document, IndexStatus
from app.models.operation import AgentOperation, DocumentRevision, OperationState
from app.rag.retriever import Retriever
from app.repositories.document import DocumentRepository
from app.repositories.operation import AgentOperationRepository, DocumentRevisionRepository
from app.schemas.agent_stream import (
    AgentDoneEvent,
    AgentRunStartedEvent,
    DraftDeltaEvent,
    OperationDraftEvent,
)
from app.schemas.agent_stream import (
    AgentStreamEvent as WireEvent,
)
from app.schemas.chat import ErrorEvent
from app.schemas.operation import (
    ApplyRequest,
    ApplyResult,
    DocumentInResult,
    DraftContent,
    OperationCreate,
    OperationReadDetail,
    OperationTransition,
    RevisionRead,
)
from app.services.agents import _as_app_error
from app.services.document import ReindexEnqueuer, _content_hash, _parse_front_matter

logger = structlog.get_logger(__name__)

_APPLICABLE_STATES = (OperationState.COMPLETED, OperationState.INTERRUPTED)

# pydantic-ai names an agent's single output tool `final_result` when it is
# registered without an explicit name — `build_draft_agent` registers
# `output_type=DraftOutput` exactly that way. Only fragments of THIS tool's
# arguments stream to the client; retrieval-tool calls stay private.
_OUTPUT_TOOL_NAME = "final_result"


class AgentOperationService:
    """Orchestrates operation creation, inspection, resume, and apply."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        enqueuer: ReindexEnqueuer | None = None,
        model: Model | None = None,
        retriever: Retriever | None = None,
    ) -> None:
        self._session = session
        self._ops = AgentOperationRepository(session)
        self._revisions = DocumentRevisionRepository(session)
        self._documents = DocumentRepository(session)
        self._enqueuer = enqueuer
        # Optional LLM wiring for the structured draft run (`draft_document_stream`);
        # None = create/inspect/apply still work, drafting 503s at call time.
        # The agent is built once per service alongside its model wiring;
        # per-run state rides in WritingDeps, never on the agent.
        self._agent: Agent[WritingDeps, DraftOutput] | None = (
            build_draft_agent(model) if model is not None and retriever is not None else None
        )
        # Kept for the per-run WritingDeps construction in `draft_document_stream`.
        self._retriever = retriever

    # --- create / inspect / resume ---

    async def create_operation(
        self, payload: OperationCreate, *, tenant_id: UUID
    ) -> OperationReadDetail:
        """Persist one completed draft operation (explicit submission path).

        Idempotent on `idempotency_key`: a retry resolves to the original
        operation instead of duplicating a draft. The draft's front matter is
        validated here — a submission that could never apply is rejected at
        creation, not at apply time.
        """
        if payload.idempotency_key is not None:
            existing = await self._ops.get_by_idempotency_key(
                payload.idempotency_key, tenant_id=tenant_id
            )
            if existing is not None:
                logger.info(
                    "operation_idempotent_create",
                    operation_id=str(existing.id),
                    idempotency_key=payload.idempotency_key,
                )
                return OperationReadDetail.model_validate(existing)

        document = await self._load_live_document(payload.document_id, tenant_id=tenant_id)
        _parse_front_matter(payload.draft.content, payload.draft.title)  # validate early
        operation = AgentOperation(
            tenant_id=tenant_id,
            document_id=document.id,
            base_document_version=payload.base_document_version,
            state=OperationState.COMPLETED,
            draft={"content": payload.draft.content, "title": payload.draft.title},
            idempotency_key=payload.idempotency_key,
        )
        try:
            operation = await self._ops.create(operation)
            await self._session.commit()
        except IntegrityError:
            # Lost a create race on the same idempotency key: return the
            # winner instead of a 500 (the unique index is the arbiter).
            await self._session.rollback()
            if payload.idempotency_key is None:
                raise
            existing = await self._ops.get_by_idempotency_key(
                payload.idempotency_key, tenant_id=tenant_id
            )
            if existing is None:
                raise
            return OperationReadDetail.model_validate(existing)
        logger.info(
            "operation_created",
            operation_id=str(operation.id),
            document_id=str(document.id),
            base_version=payload.base_document_version.isoformat(),
        )
        return OperationReadDetail.model_validate(operation)

    async def get_operation(self, operation_id: UUID, *, tenant_id: UUID) -> OperationReadDetail:
        """Inspect one operation, draft payload included."""
        operation = await self._get_or_raise(operation_id, tenant_id=tenant_id)
        return OperationReadDetail.model_validate(operation)

    async def list_operations(
        self, document_id: UUID, *, tenant_id: UUID
    ) -> list[OperationReadDetail]:
        """One document's operations, newest first (explicit audit view)."""
        await self._load_live_document(document_id, tenant_id=tenant_id)
        rows = await self._ops.list_for_document(document_id, tenant_id=tenant_id)
        return [OperationReadDetail.model_validate(row) for row in rows]

    async def resume_operation(
        self, operation_id: UUID, payload: OperationTransition, *, tenant_id: UUID
    ) -> OperationReadDetail:
        """Explicitly resume an interrupted (or failed) operation.

        Marks it completed — optionally with an amended draft — so it becomes
        applicable. Running and already-applied operations cannot resume.
        """
        operation = await self._get_or_raise(operation_id, tenant_id=tenant_id)
        if operation.state not in (OperationState.INTERRUPTED, OperationState.FAILED):
            raise ConflictError(
                f"Operation {operation_id} cannot be resumed from state '{operation.state.value}'",
                details={"state": operation.state.value},
            )
        if payload.draft is not None:
            _parse_front_matter(payload.draft.content, payload.draft.title)
            operation.draft = {"content": payload.draft.content, "title": payload.draft.title}
        operation.state = OperationState.COMPLETED
        operation.error = None
        operation = await self._ops.update(operation)
        await self._session.commit()
        logger.info("operation_resumed", operation_id=str(operation.id))
        return OperationReadDetail.model_validate(operation)

    # --- apply (atomic, optimistic concurrency, idempotent) ---

    async def apply_operation(
        self, operation_id: UUID, payload: ApplyRequest, *, tenant_id: UUID
    ) -> ApplyResult:
        """Publish a draft as one document revision — the only publish path.

        Guards, in order, all before any mutation: the operation must be
        completed or interrupted (a running/failed draft cannot publish); an
        already-applied operation returns its original revision (duplicate
        apply is idempotent, never a second revision); the live document's
        `updated_at` must equal the draft's base version (stale apply is
        rejected with a 409 and zero writes). The write itself is ONE
        transaction: document update, revision row, and `applied` state
        commit together; indexing is enqueued only after that commit, so an
        enqueue failure leaves durable content `pending` for the retry sweep.
        """
        operation = await self._get_or_raise(operation_id, tenant_id=tenant_id)

        if operation.state is OperationState.APPLIED:
            return await self._applied_result(operation, tenant_id=tenant_id)

        if operation.state not in _APPLICABLE_STATES:
            raise ConflictError(
                f"Operation {operation_id} is '{operation.state.value}' and cannot be applied",
                details={"state": operation.state.value},
            )
        if operation.draft is None:
            raise ConflictError(
                f"Operation {operation_id} has no draft to apply",
                details={"state": operation.state.value},
            )
        if (
            payload.expected_base_document_version is not None
            and payload.expected_base_document_version != operation.base_document_version
        ):
            raise ConflictError(
                "Expected base version does not match the operation's recorded base",
                details={
                    "expected": payload.expected_base_document_version.isoformat(),
                    "base": operation.base_document_version.isoformat()
                    if operation.base_document_version
                    else None,
                },
            )

        document = await self._load_live_document(operation.document_id, tenant_id=tenant_id)
        if (
            operation.base_document_version is None
            or document.updated_at != operation.base_document_version
        ):
            # Stale apply: the document moved on after the draft was made.
            # Nothing has been written at this point — reject and mutate none.
            logger.warning(
                "operation_apply_stale",
                operation_id=str(operation.id),
                document_id=str(document.id),
                base_version=operation.base_document_version.isoformat()
                if operation.base_document_version
                else None,
                current_version=document.updated_at.isoformat(),
            )
            raise ConflictError(
                "Document changed since the draft was created; re-draft or resume the operation",
                details={
                    "base_version": operation.base_document_version.isoformat()
                    if operation.base_document_version
                    else None,
                    "current_version": document.updated_at.isoformat(),
                },
            )

        draft = DraftContent.model_validate(operation.draft)
        title, tags = _parse_front_matter(draft.content, draft.title)
        document.content = draft.content
        document.title = title
        document.tags = tags
        document.content_hash = _content_hash(draft.content)
        document.index_status = IndexStatus.PENDING
        await self._documents.update(document)

        revision = await self._revisions.create(
            DocumentRevision(
                tenant_id=operation.tenant_id,
                document_id=document.id,
                operation_id=operation.id,
                title=title,
                content=draft.content,
                tags=tags,
            )
        )
        operation.state = OperationState.APPLIED
        operation.result = {"revision_id": str(revision.id)}
        operation = await self._ops.update(operation)

        await self._session.commit()
        logger.info(
            "operation_applied",
            operation_id=str(operation.id),
            document_id=str(document.id),
            revision_id=str(revision.id),
            base_version=operation.base_document_version.isoformat()
            if operation.base_document_version
            else None,
        )
        # After commit only — a rolled-back apply must never be indexed. The
        # document stays `pending` if enqueueing fails; the CLI reindex sweep
        # (or a re-save) finishes it — durable content, never lost.
        if self._enqueuer is not None:
            self._enqueuer(document.id, document.tenant_id, document.updated_at)
        return ApplyResult(
            operation=OperationReadDetail.model_validate(operation),
            revision=RevisionRead.model_validate(revision),
            document=DocumentInResult.model_validate(document),
        )

    async def _applied_result(self, operation: AgentOperation, *, tenant_id: UUID) -> ApplyResult:
        """Idempotent repeat of an already-applied operation: read back the
        original revision; no second revision is ever created."""
        revision_id = operation.result.get("revision_id") if operation.result else None
        revision = (
            await self._revisions.get_by_id(UUID(str(revision_id)), tenant_id=tenant_id)
            if revision_id is not None
            else None
        )
        if revision is None:
            revision = await self._revisions.get_by_operation_id(operation.id, tenant_id=tenant_id)
        if revision is None:
            raise ConflictError(
                f"Operation {operation.id} is applied but has no revision record",
                details={"state": operation.state.value},
            )
        document = await self._load_live_document(operation.document_id, tenant_id=tenant_id)
        logger.info("operation_apply_idempotent", operation_id=str(operation.id))
        return ApplyResult(
            operation=OperationReadDetail.model_validate(operation),
            revision=RevisionRead.model_validate(revision),
            document=DocumentInResult.model_validate(document),
        )

    # --- structured draft run (writing agent wiring) ---

    async def draft_document_stream(
        self, document_id: UUID, instruction: str | None, *, tenant_id: UUID, limit: int = 8
    ) -> AsyncIterator[WireEvent]:
        """Stream one structured draft run as typed events (design: agent stream).

        Contract order: `run_started` → `draft_delta`* (raw output-tool JSON
        fragments streamed verbatim while generation is in flight) → `draft`
        (operation_id, state, flat draft content) → `done`. Deltas are
        informational: the server never parses partial JSON, and a client
        discards them when the stream ends in the terminal `error`.
        Persistence timing is preserved from the
        synchronous endpoint: the `running` row commits BEFORE the model call
        (a lost process leaves an inspectable, resumable record), and the
        terminal state (`completed`/`failed` + error details) commits BEFORE
        the corresponding terminal event — a client that stops reading never
        sees success for unpersisted work. The stream ends with the draft,
        never an apply.

        Pre-stream failures (drafting not configured → 503, missing/soft-
        deleted document → 404) raise before the first yield so the priming
        endpoint keeps its HTTP envelopes; everything after that becomes a
        terminal `error` event — nothing raises out of this generator once
        it has yielded.
        """
        yielded = False
        try:
            async for event in self._draft_events(
                document_id, instruction, tenant_id=tenant_id, limit=limit
            ):
                yielded = True
                yield event
        except AppError as failure:
            if not yielded:
                raise  # pre-stream (config gate / document load): envelope applies
            yield ErrorEvent(code=failure.code, message=failure.message)

    async def _draft_events(
        self, document_id: UUID, instruction: str | None, *, tenant_id: UUID, limit: int
    ) -> AsyncIterator[WireEvent]:
        """Internal event generator; raises `AppError` on any failure."""
        started = time.perf_counter()
        # The configuration gate and the document load come before anything
        # else: both must stay clean HTTP errors, and no operation row is
        # created for a request that could never run.
        if self._agent is None:
            raise ChatUnavailableError(
                "Drafting is not configured: set CHAT_API_KEY to enable it",
            )
        document = await self._load_live_document(document_id, tenant_id=tenant_id)
        operation = await self._ops.create(
            AgentOperation(
                tenant_id=tenant_id,
                document_id=document.id,
                base_document_version=document.updated_at,
                state=OperationState.RUNNING,
            )
        )
        await self._session.commit()
        run_id = uuid4().hex
        log = logger.bind(
            operation_id=str(operation.id), document_id=str(document.id), run_id=run_id
        )
        log.info("operation_draft_started")
        yield AgentRunStartedEvent(run_id=run_id, kind="draft", document_id=document.id)

        collector = SourceCollector()
        assert self._retriever is not None  # guaranteed by the _agent wiring
        deps = WritingDeps(
            retriever=self._retriever, limit=limit, collector=collector, tenant_id=tenant_id
        )
        try:
            # Streaming keeps bytes flowing through the gateway, whose
            # non-streaming requests face a hard whole-request deadline no
            # full draft generation can beat. `agent.iter()` exposes the raw
            # stream events: every output-tool argument fragment is forwarded
            # to the client as a `draft_delta` event, verbatim and in order,
            # while the run is still in flight. The validated `DraftOutput`
            # itself only lands in the operation after the run completes.
            part_names: dict[int, str] = {}
            forwarded: set[int] = set()
            async with self._agent.iter(
                render_writing_prompt(document.content, instruction), deps=deps
            ) as run:
                async for node in run:
                    if not self._agent.is_model_request_node(node):
                        continue  # tool/output handling needs no wire event
                    async with node.stream(run.ctx) as stream:
                        async for event in stream:
                            wire_delta = _output_tool_delta(
                                run_id, event, part_names, forwarded
                            )
                            if wire_delta is not None:
                                yield wire_delta
                run_result = run.result
                assert run_result is not None  # iteration always ends at the result
                output = run_result.output
        except Exception as exc:
            failure = _as_app_error(exc)
            # Terminal state commits BEFORE the terminal event (the wrapper
            # turns this raise into the single `error`): the failed operation
            # is durable and resumable even if the client stops reading.
            operation.state = OperationState.FAILED
            operation.error = {"error_class": type(exc).__name__}
            await self._ops.update(operation)
            await self._session.commit()
            log.exception(
                "operation_draft_failed",
                outcome=failure.code,
                error_class=type(exc).__name__,
            )
            raise failure from exc

        operation.draft = _draft_payload(output)
        operation.state = OperationState.COMPLETED
        operation = await self._ops.update(operation)
        await self._session.commit()
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.info("operation_draft_finished", tool_calls=collector.tool_calls, latency_ms=latency_ms)
        yield OperationDraftEvent(
            operation_id=operation.id,
            state="completed",
            content=output.content,
            title=output.title,
        )
        yield AgentDoneEvent(run_id=run_id, outcome="success", latency_ms=latency_ms)

    # --- internals ---

    async def _load_live_document(self, document_id: UUID | None, *, tenant_id: UUID) -> Document:
        """Fetch one live document in the tenant scope; soft-deleted counts as
        missing."""
        if document_id is None:
            raise NotFoundError("Operation has no target document")
        document = await self._documents.get_by_id(document_id, tenant_id=tenant_id)
        if document is None:
            raise NotFoundError(f"Document {document_id} not found")
        return document

    async def _get_or_raise(self, operation_id: UUID, *, tenant_id: UUID) -> AgentOperation:
        operation = await self._ops.get_by_id(operation_id, tenant_id=tenant_id)
        if operation is None:
            # Non-leaky 404: another tenant's operation is "not found".
            raise NotFoundError(f"Operation {operation_id} not found")
        return operation


def _draft_payload(output: DraftOutput) -> dict[str, object]:
    """Structured agent output -> the operation's JSONB draft shape."""
    return {"content": output.content, "title": output.title}


def _output_tool_delta(
    run_id: str,
    event: AgentStreamEvent,
    part_names: dict[int, str],
    forwarded: set[int],
) -> DraftDeltaEvent | None:
    """Translate one raw stream event into a `draft_delta`, or None.

    Only the structured-output tool's argument fragments are surfaced, in
    order and verbatim (retrieval-tool calls are skipped). `part_names`
    accumulates streamed tool names per part index (names can arrive in
    fragments too); `forwarded` keeps each fragment exactly-once even if the
    framework upgrades a buffered delta into a full part whose args would
    otherwise repeat already-forwarded fragments.
    """
    if isinstance(event, PartStartEvent):
        part = event.part
        if not isinstance(part, ToolCallPart):
            return None
        part_names[event.index] = part.tool_name
        # A part can start before any argument bytes arrive (`args is None`);
        # its `{}` serialization is not a real fragment and must not stream.
        args_json = part.args_as_json_str() if part.args is not None else ""
        if (
            part.tool_name == _OUTPUT_TOOL_NAME
            and args_json
            and event.index not in forwarded
        ):
            forwarded.add(event.index)
            return DraftDeltaEvent(run_id=run_id, delta=args_json)
        return None
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, ToolCallPartDelta):
        delta = event.delta
        name = part_names.get(event.index, "") + (delta.tool_name_delta or "")
        if name:
            part_names[event.index] = name
        if (
            name == _OUTPUT_TOOL_NAME
            and isinstance(delta.args_delta, str)
            and delta.args_delta
        ):
            forwarded.add(event.index)
            return DraftDeltaEvent(run_id=run_id, delta=delta.args_delta)
    return None
