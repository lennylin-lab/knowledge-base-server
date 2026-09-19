"""Shared FastAPI dependencies for the v1 API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, ClassVar, cast
from uuid import UUID

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import BackgroundTasks, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dependencies import get_token_verifier, require_bearer
from app.auth.rbac import Permission, ensure_allowed
from app.auth.verifier import TokenVerifier
from app.core.cache import Cache, NullCache, RedisCache
from app.core.config import Settings, get_settings
from app.core.database import SessionFactory, get_db
from app.core.exceptions import (
    AuthenticationError,
    ChatUnavailableError,
    ForbiddenError,
    TenantUnavailableError,
)
from app.llm.discovery import ModelFacts, discover
from app.llm.embeddings import CachingEmbeddingProvider, EmbeddingProvider, OpenAIEmbeddingProvider
from app.llm.models import get_chat_model
from app.llm.profile import RETRIEVER_THRESHOLD_KEYS, apply_profile
from app.mcp.manager import get_mcp_manager
from app.mcp.tools import build_agent_tools
from app.models.document_chunk import EMBEDDING_DIM as PGVECTOR_EMBEDDING_DIM
from app.models.tenant import MembershipRole, MembershipStatus, UserStatus
from app.rag.indexer import run_indexing
from app.rag.retriever import Retriever
from app.rag.worker import INDEX_DOCUMENT_TASK
from app.repositories.tenant import TenantMembershipRepository, TenantRepository, UserRepository
from app.search.es import get_shared_es_client
from app.services.agents import AssociationService, SummarizeService, WritingService
from app.services.chat import ChatService
from app.services.document import DocumentService, ReindexEnqueuer
from app.services.operation import AgentOperationService
from app.services.search import SearchService
from app.services.session import ChatSessionService

logger = structlog.get_logger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_db)]


# --- model control plane (gateway v1.3, resolved once at startup) ---
#
# The effective chat model / embedding dim / retrieval profile are resolved
# in the app lifespan (`resolve_model_control_plane`) and cached for the
# process lifetime (pydantic-ai requires a concrete model name; gateway-side
# default or profile changes need a restart — documented). Builders read the
# effective values so `KB_CHAT_MODEL`-set deployments behave byte-identically
# to before, while unset deployments use the discovered default.

_resolved_chat_model: str | None = None
_resolved_embedding_dim: int | None = None
_resolved_profile: Mapping[str, object] = {}


def reset_model_control_plane() -> None:
    """Clear the startup-resolved state (app restarts, tests)."""
    global _resolved_chat_model, _resolved_embedding_dim, _resolved_profile
    _resolved_chat_model = None
    _resolved_embedding_dim = None
    _resolved_profile = {}


def configured_chat_model(settings: Settings) -> str | None:
    """The env-configured chat model, normalized: `KB_CHAT_MODEL=` (the
    common shell way to "unset" a var) parses as the empty string, not None
    — whitespace-only values are treated exactly like unset everywhere the
    env model is consulted (discovery probe path, fail-fast, env-wins
    precedence)."""
    if settings.CHAT_MODEL is None:
        return None
    return settings.CHAT_MODEL.strip() or None


def effective_chat_model(settings: Settings) -> str:
    """The chat model name every consumer must use: env wins (byte-identical
    legacy behavior), then the startup-discovered gateway default, then the
    retired hard default for direct construction before startup resolution
    (tests, tooling). Unset + failed discovery is refused earlier, at startup
    (`resolve_model_control_plane`)."""
    env_model = configured_chat_model(settings)
    if env_model is not None:
        return env_model
    if _resolved_chat_model is not None:
        return _resolved_chat_model
    return settings.DEFAULT_CHAT_MODEL


def effective_embedding_dim(settings: Settings) -> int:
    """Discovered `embedding_dim` wins; `KB_EMBEDDING_DIM` is the fallback,
    then the pgvector column width (an unset/empty dim means "discover")."""
    if _resolved_embedding_dim is not None:
        return _resolved_embedding_dim
    if settings.EMBEDDING_DIM is not None:
        return settings.EMBEDDING_DIM
    return PGVECTOR_EMBEDDING_DIM


async def resolve_model_control_plane(settings: Settings, *, client: object | None = None) -> None:
    """Startup resolution (app lifespan): discover model facts from the
    gateway, thread effective values into the module globals, cross-check the
    embedding dim against the pgvector column width, and log every source
    (`model_control_plane_resolved` — identifiers only, never keys/content).

    `client` is a test seam (AsyncOpenAI over a MockTransport). Fails startup
    ONLY when `KB_CHAT_MODEL` is unset and the gateway default is
    undiscoverable — the gateway would 400 every chat request anyway.
    """
    global _resolved_chat_model, _resolved_embedding_dim, _resolved_profile
    reset_model_control_plane()
    if not settings.CHAT_API_KEY.get_secret_value():
        # Chat is unavailable anyway (every builder 503s on the empty key):
        # no gateway call, straight to fallbacks — keeps LLM-less
        # deployments (and the offline test suite) free of discovery traffic.
        logger.info(
            "model_control_plane_resolved",
            chat_model=settings.DEFAULT_CHAT_MODEL,
            chat_model_source="unconfigured",
            embedding_dim=effective_embedding_dim(settings),
            embedding_dim_source="env",
            thresholds={key: "env" for key in RETRIEVER_THRESHOLD_KEYS},
        )
        return
    facts: ModelFacts | None = await discover(settings, client=cast("Any | None", client))
    chat_model_source = "env"
    if configured_chat_model(settings) is None:
        if facts is None:
            raise RuntimeError(
                "KB_CHAT_MODEL is not set and the gateway default chat model "
                "could not be discovered; set KB_CHAT_MODEL or verify the "
                "gateway's default-model policy is configured"
            )
        _resolved_chat_model = facts.model_name
        chat_model_source = "discovered"
    embedding_dim_source = "env"
    if facts is not None:
        if facts.embedding_dim is not None:
            _resolved_embedding_dim = facts.embedding_dim
            embedding_dim_source = "discovered"
        _resolved_profile = facts.retrieval_profile
    effective_dim = effective_embedding_dim(settings)
    if effective_dim != PGVECTOR_EMBEDDING_DIM:
        # Cross-check only: the mismatch actually fails at write time (the
        # gateway's embedding_dim_mismatch / the pgvector insert); this
        # warning makes the cause visible at boot instead.
        logger.warning(
            "embedding_dim_mismatch",
            effective_dim=effective_dim,
            embedding_dim_source=embedding_dim_source,
            pgvector_column_width=PGVECTOR_EMBEDDING_DIM,
        )
    thresholds = {key: "env" for key in RETRIEVER_THRESHOLD_KEYS}
    thresholds.update({key: "profile" for key in apply_profile({}, _resolved_profile)})
    logger.info(
        "model_control_plane_resolved",
        chat_model=effective_chat_model(settings),
        chat_model_source=chat_model_source,
        embedding_dim=effective_dim,
        embedding_dim_source=embedding_dim_source,
        thresholds=thresholds,
    )


async def _resolve_default_tenant(session: AsyncSession, settings: Settings) -> UUID:
    """Resolve the configured default tenant from persistence (shared tail of
    both scope paths). A missing tenant row means an un-migrated database:
    a clean 503, never a silent all-tenants fallback."""
    tenant = await TenantRepository(session).get_by_slug(settings.TENANT_DEFAULT_SLUG)
    if tenant is None:
        logger.error("tenant_unavailable", slug=settings.TENANT_DEFAULT_SLUG)
        raise TenantUnavailableError(
            "Tenant scope is not available; run database migrations",
        )
    return tenant.id


async def _resolve_membership_scope(
    session: AsyncSession,
    settings: Settings,
    authorization: str | None,
    verifier: TokenVerifier,
) -> tuple[UUID, MembershipRole]:
    """OIDC path: verified principal -> membership -> (tenant, role).

    Claim mapping (design.md): the ONLY identity claim is `sub` — tenant and
    role are resolved exclusively from server persistence (`users.subject` ->
    `tenant_memberships`), never from token claims or headers, so an IdP
    cannot grant membership the server did not record. Failure policy:
    verification failures are 401 (authn); an authenticated caller with no
    usable membership here is 403 (authz) — cross-tenant resource ids stay
    non-leaky 404s downstream.
    """
    token = require_bearer(authorization)
    principal = await verifier.verify(token)  # generic 401 on any failure
    tenant_id = await _resolve_default_tenant(session, settings)
    user = await UserRepository(session).get_by_subject(principal.subject_id)
    membership = None
    if user is not None and user.status == UserStatus.ACTIVE:
        membership = await TenantMembershipRepository(session).get_membership(tenant_id, user.id)
    if (
        membership is None
        or membership.status != MembershipStatus.ACTIVE
        or membership.role == MembershipRole.SERVICE_ACCOUNT
    ):
        logger.warning("membership_rejected", subject_known=user is not None)
        raise ForbiddenError("You do not have access to this tenant")
    return tenant_id, membership.role


async def get_tenant_scope(
    request: Request,
    session: SessionDep,
    verifier: Annotated[TokenVerifier | None, Depends(get_token_verifier)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> UUID:
    """Resolve the request's tenant scope and (when OIDC is on) its role.

    Two explicit paths, per design.md:

    - Compatibility path (default; `KB_OIDC_ISSUER` empty): single-user MVP —
      every request is scoped to the configured default tenant
      (`KB_TENANT_DEFAULT_SLUG`, backfilled by migration 0010), resolved from
      server persistence, never from a client header. No role is attached;
      downstream permission checks are skipped. This path is kept for the MVP
      deliberately (design.md rollout step 6 enables RBAC by configuration);
      tests pin both paths.
    - Membership path (`KB_OIDC_ISSUER` set): the bearer token is verified
      (signature/iss/aud/exp/JWKS) and the role comes from
      `tenant_memberships` — see `_resolve_membership_scope`. Service-account
      keys are NOT accepted here (they grant only internal routes).
    """
    settings = get_settings()
    if not settings.OIDC_ISSUER:
        tenant_id = await _resolve_default_tenant(session, settings)
        request.state.membership_role = None  # compatibility: no RBAC
        return tenant_id
    if verifier is None:
        # Issuer configured but no verifier could be built: deny, never
        # fall open onto the compatibility path.
        logger.error("auth_verifier_unavailable")
        raise AuthenticationError("Authentication required")
    tenant_id, role = await _resolve_membership_scope(session, settings, authorization, verifier)
    request.state.membership_role = role
    return tenant_id


def tenant_scope_requiring(
    *permissions: Permission,
) -> Callable[[Request, UUID], Coroutine[Any, Any, UUID]]:
    """Build a scope dependency that additionally enforces the RBAC matrix.

    The returned dependency depends on `get_tenant_scope` (so overrides in
    tests and the compatibility path keep working) and then checks the role
    the scope resolver attached to `request.state` against every requested
    permission. No role attached (compatibility mode) means no check — the
    MVP actor stands in for every role.
    """

    async def dep(
        request: Request,
        scope: Annotated[UUID, Depends(get_tenant_scope)],
    ) -> UUID:
        role = getattr(request.state, "membership_role", None)
        if role is not None:
            resolved = MembershipRole(role)
            for permission in permissions:
                ensure_allowed(resolved, permission)
        return scope

    return dep


TenantScope = Annotated[UUID, Depends(get_tenant_scope)]
DocumentWriteScope = Annotated[UUID, Depends(tenant_scope_requiring(Permission.DOCUMENT_WRITE))]
SessionWriteScope = Annotated[UUID, Depends(tenant_scope_requiring(Permission.SESSION_WRITE))]
ChatScope = Annotated[UUID, Depends(tenant_scope_requiring(Permission.CHAT_CREATE))]
OperationCreateScope = Annotated[UUID, Depends(tenant_scope_requiring(Permission.OPERATION_CREATE))]
OperationApplyScope = Annotated[UUID, Depends(tenant_scope_requiring(Permission.OPERATION_APPLY))]


def get_document_service(session: SessionDep, background_tasks: BackgroundTasks) -> DocumentService:
    """One service per request, sharing the request's session.

    The FastAPI adapter for the write-path indexing trigger: the service only
    sees an injected enqueuer; this is the sole place BackgroundTasks appears.
    The cache handle is process-lifetime and only used for the best-effort
    search-epoch bump after each committed write.
    """
    return DocumentService(
        session, enqueuer=make_index_enqueuer(background_tasks), cache=get_cache()
    )


# --- shared cache handle (opt-in, best-effort Redis) ---

_shared_cache: Cache | None = None


def get_cache() -> Cache:
    """The one cache per process, built lazily (the `_get_shared_arq_pool`
    pattern). Effective enable = CACHE_ENABLED AND non-empty REDIS_URL — the
    default empty REDIS_URL wires a shared `NullCache`: no redis client is
    ever constructed and behavior is byte-identical to no cache. Tests may
    pre-set the module global to inject a stub."""
    global _shared_cache
    if _shared_cache is None:
        settings = get_settings()
        if settings.CACHE_ENABLED and settings.REDIS_URL:
            _shared_cache = RedisCache.from_url(settings.REDIS_URL)
        else:
            _shared_cache = NullCache()
    return _shared_cache


async def close_cache() -> None:
    """App-shutdown hook: close the shared cache client if this process
    built one (a no-op in NullCache mode)."""
    global _shared_cache
    if _shared_cache is not None:
        await _shared_cache.aclose()
        _shared_cache = None


# --- indexing enqueue transport (BackgroundTasks | ARQ) ---


def make_index_enqueuer(background_tasks: BackgroundTasks) -> ReindexEnqueuer:
    """Pick the indexing transport once, at service construction.

    Empty `REDIS_URL` (the default) keeps today's in-process BackgroundTasks
    path exactly — no Redis client is ever constructed. A configured URL
    routes enqueueing to the shared ARQ pool instead; BackgroundTasks then
    carries nothing (it stays solely the fallback's transport). Both paths
    carry the document's tenant id (the background run's scope) and its
    `updated_at` version stamp so the pipeline's generation guard can skip
    superseded jobs.
    """
    if not get_settings().REDIS_URL:
        return lambda doc_id, tenant_id, updated_at: background_tasks.add_task(
            run_indexing, doc_id, tenant_id, updated_at
        )
    return _ArqEnqueuer()


_shared_arq_pool: ArqRedis | None = None


async def _get_shared_arq_pool() -> ArqRedis:
    """The one ARQ pool per process, built lazily on first enqueue (the
    `get_shared_es_client` pattern): enqueuers are constructed per request
    but must never open a pool per request. Tests pre-set the module global
    to stub the pool."""
    global _shared_arq_pool
    if _shared_arq_pool is None:
        _shared_arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().REDIS_URL))
    return _shared_arq_pool


async def close_arq_pool() -> None:
    """App-shutdown hook: close the shared pool if this process built one
    (a no-op in BackgroundTasks mode — the pool never exists)."""
    global _shared_arq_pool
    if _shared_arq_pool is not None:
        await _shared_arq_pool.aclose(close_connection_pool=True)
        _shared_arq_pool = None


class _ArqEnqueuer:
    """Enqueues indexing jobs on the shared ARQ pool.

    `__call__` keeps the sync `ReindexEnqueuer` shape the service expects;
    the async enqueue runs as a fire-and-forget task on the request's event
    loop. A Redis problem at enqueue time degrades loudly but safely: the
    write itself is already committed, so `index_enqueue_failed` is warned,
    the document stays `pending`, and the CLI reindex sweep finishes it —
    the CRUD response never 5xx-es over a queue hiccup.
    """

    # A live reference per fire-and-forget task, or the GC may collect it
    # mid-flight; done-callbacks keep the set bounded.
    _inflight: ClassVar[set[asyncio.Task[None]]] = set()

    def __call__(self, doc_id: UUID, tenant_id: UUID, updated_at: datetime) -> None:
        task = asyncio.create_task(self._enqueue(doc_id, tenant_id, updated_at))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _enqueue(self, doc_id: UUID, tenant_id: UUID, updated_at: datetime) -> None:
        try:
            pool = await _get_shared_arq_pool()
            # The tenant id and ISO version stamp ride in the payload
            # (JSON-safe); the worker scopes the run by tenant and degrades
            # unparseable/absent version values to no guard.
            await pool.enqueue_job(
                INDEX_DOCUMENT_TASK, str(doc_id), str(tenant_id), updated_at.isoformat()
            )
            logger.info("index_enqueued", document_id=str(doc_id), mode="arq")
        except Exception as exc:
            logger.warning(
                "index_enqueue_failed",
                document_id=str(doc_id),
                error_class=type(exc).__name__,
            )


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


def get_chat_session_service(session: SessionDep) -> ChatSessionService:
    """One service per request, sharing the request's session."""
    return ChatSessionService(session)


ChatSessionServiceDep = Annotated[ChatSessionService, Depends(get_chat_session_service)]


def embedding_provider_from_settings(settings: Settings) -> EmbeddingProvider | None:
    """Build the embedding provider only when an API key is configured.

    `None` means BM25-only search — a user-visible degradation mode, not a
    failure (see design.md); the accompanying warning is emitted by
    `build_search_service` so it fires once per construction. When the cache
    is effectively enabled the provider is wrapped in the caching decorator
    (per-text vectors, covering search-query and indexing embedding alike);
    the disabled mode returns the raw provider — byte-identical call path.
    """
    if not settings.EMBEDDING_API_KEY.get_secret_value():
        return None
    dim = effective_embedding_dim(settings)
    provider: EmbeddingProvider = OpenAIEmbeddingProvider.from_settings(settings, dimensions=dim)
    if settings.CACHE_ENABLED and settings.REDIS_URL:
        provider = CachingEmbeddingProvider(
            provider,
            get_cache(),
            model=settings.EMBEDDING_MODEL,
            dim=dim,
            ttl_seconds=settings.CACHE_EMBEDDING_TTL_S,
        )
    return provider


def _build_retriever(settings: Settings, provider: EmbeddingProvider | None) -> Retriever:
    """Shared retriever wiring for every retrieval-backed service.

    The relevance gate thresholds flow from Settings so operators can tune
    (or disable — see the sentinels in `core/config.py`) without code changes;
    every retrieval-backed consumer (search, chat agents) shares them. The
    gateway's `retrieval_profile` (startup-discovered) overrides the matching
    env values at this single choke point — precedence: profile > env >
    `Retriever` module defaults (`llm/profile.py`).
    """
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory
    thresholds = apply_profile(
        {
            "bm25_min_score": settings.SEARCH_BM25_MIN_SCORE,
            "bm25_min_coverage": settings.SEARCH_BM25_MIN_COVERAGE,
            "vector_max_distance": settings.SEARCH_VECTOR_MAX_DISTANCE,
            "vector_rescue_margin": settings.SEARCH_VECTOR_RESCUE_MARGIN,
            "vector_rescue_max_distance": settings.SEARCH_VECTOR_RESCUE_MAX_DISTANCE,
            "vector_rescue_trigger_max_distance": (
                settings.SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE
            ),
            "rrf_min_relative": settings.SEARCH_RRF_MIN_RELATIVE,
            "max_query_length": settings.SEARCH_MAX_QUERY_LENGTH,
        },
        _resolved_profile,
    )
    return Retriever(
        session_factory=session_factory,
        es_client=get_shared_es_client(),
        embedding_provider=provider,
        es_index=settings.ES_INDEX,
        bm25_min_score=cast("float", thresholds["bm25_min_score"]),
        bm25_min_coverage=cast("str", thresholds["bm25_min_coverage"]),
        vector_max_distance=cast("float", thresholds["vector_max_distance"]),
        vector_rescue_margin=cast("float", thresholds["vector_rescue_margin"]),
        vector_rescue_max_distance=cast("float", thresholds["vector_rescue_max_distance"]),
        vector_rescue_trigger_max_distance=cast(
            "float", thresholds["vector_rescue_trigger_max_distance"]
        ),
        rrf_min_relative=cast("float", thresholds["rrf_min_relative"]),
        max_query_length=cast("int", thresholds["max_query_length"]),
        cache=get_cache(),
        cache_ttl_seconds=settings.CACHE_SEARCH_TTL_S,
    )


def build_search_service(settings: Settings) -> SearchService:
    """Wire the search service from Settings (uncached constructor)."""
    provider = embedding_provider_from_settings(settings)
    if provider is None:
        logger.warning("vector_search_disabled", reason="embedding_api_key_not_configured")
    return SearchService(_build_retriever(settings, provider))


@lru_cache(maxsize=1)
def get_search_service() -> SearchService:
    """Cached accessor: one retriever/service (and one degradation warning)
    per process; the ES transport is shared via `search.es`, never opened
    per request."""
    return build_search_service(get_settings())


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]


def build_chat_service(settings: Settings) -> ChatService:
    """Wire the chat service from Settings (uncached constructor).

    Chat has no non-LLM fallback: without an API key this raises
    `ChatUnavailableError` BEFORE any stream can start (a clean 503 envelope,
    never a half-open stream). The retrieval leg may still degrade to BM25 at
    search time — that is the retriever's own per-run behavior.

    Ordering note: FastAPI resolves dependencies before body validation, so an
    unconfigured deployment answers 503 `chat_unavailable` even for invalid
    request bodies — the same contract as any gate-style (e.g. auth)
    dependency. The 422 request-schema contract holds whenever the service is
    constructible (key present).
    """
    if not settings.CHAT_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Chat is not configured: set CHAT_API_KEY to enable it",
        )
    # External MCP tools ride along when the (process-lifetime) manager is up
    # with a non-empty tool snapshot; unconfigured deployments build none and
    # the agent is exactly the pre-MCP one. The snapshot is taken once here —
    # config changes need a restart (documented, no hot reload).
    manager = get_mcp_manager()
    extra_tools = build_agent_tools(manager, manager.list_tools()) if manager.running else []
    # Since provider-config isolation the embedding key is independent of
    # CHAT_API_KEY: chat with only a chat key wires a BM25-only retriever,
    # and run_started.mode must report that truthfully (writing's pattern).
    provider = embedding_provider_from_settings(settings)
    model = get_chat_model(settings, effective_chat_model(settings))
    return ChatService(
        _build_retriever(settings, provider),
        model,
        mode="hybrid" if provider is not None else "bm25",
        extra_tools=extra_tools,
        # Session persistence: the process-lifetime service opens one session
        # per ask() via the factory (the SummarizeService lifetime pattern).
        session_factory=SessionFactory,
        # Token-based history budget + per-turn guardrail fraction; the
        # token counter is built (once, lazily) from CHAT_MODEL inside the
        # service — production never injects one.
        history_token_budget=settings.CHAT_HISTORY_TOKEN_BUDGET,
        history_max_turn_fraction=settings.CHAT_HISTORY_MAX_TURN_FRACTION,
        # Follow-up rewrite shares the single chat model/SDK client; the
        # Settings flag is the runtime kill switch (false = no rewrite at all).
        rewrite_model=model if settings.CHAT_QUERY_REWRITE_ENABLED else None,
        rewrite_history_turns=settings.CHAT_REWRITE_HISTORY_TURNS,
        # Rolling summary of evicted history shares the same model instance;
        # its flag is the runtime kill switch back to cliff eviction (false =
        # no summary consulted, computed, or injected).
        summary_model=model if settings.CHAT_ROLLING_SUMMARY_ENABLED else None,
        summary_max_tokens=settings.CHAT_SUMMARY_MAX_TOKENS,
        # Carry prior-run sources into follow-ups; the flag is the runtime
        # kill switch (false = no sources write, emission, or preamble).
        carry_sources_forward=settings.CHAT_SOURCES_CARRY_ENABLED,
    )


@lru_cache(maxsize=1)
def get_chat_service() -> ChatService:
    """Cached accessor, like `get_search_service`: one model, one SDK client
    per process. `lru_cache` never memoizes raised exceptions, so the no-key
    path still fails every request with `ChatUnavailableError`."""
    return build_chat_service(get_settings())


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]


def build_summarize_service(settings: Settings) -> SummarizeService:
    """Wire the summarize service from Settings (uncached constructor).

    Same no-key gate as chat: without `CHAT_API_KEY` this raises
    `ChatUnavailableError` before any document load or model call. Summarize
    has no non-LLM fallback either, so it reuses chat's error (503
    `chat_unavailable`) rather than inventing a near-identical one. The
    configured `CHAT_MODEL` name is passed alongside the model so responses
    echo it even when tests inject a FunctionModel stand-in.
    """
    if not settings.CHAT_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Summarize is not configured: set CHAT_API_KEY to enable it",
        )
    return SummarizeService(
        get_chat_model(settings, effective_chat_model(settings)),
        effective_chat_model(settings),
        session_factory=SessionFactory,
        cache=get_cache(),
        cache_ttl_seconds=settings.CACHE_SUMMARY_TTL_S,
        summary_max_tokens=settings.DOCUMENT_SUMMARY_MAX_TOKENS,
        summary_chunk_target=settings.DOCUMENT_SUMMARY_CHUNK_TARGET,
        summary_chunk_max_size=settings.DOCUMENT_SUMMARY_CHUNK_MAX_SIZE,
    )


@lru_cache(maxsize=1)
def get_summarize_service() -> SummarizeService:
    """Cached accessor like `get_chat_service`: one model and SDK client per
    process. The service opens its own session per run via the injected
    session factory (the Retriever pattern), so it needs no request-scoped
    session — and the no-key path still fails every request."""
    return build_summarize_service(get_settings())


SummarizeServiceDep = Annotated[SummarizeService, Depends(get_summarize_service)]


def build_association_service(settings: Settings) -> AssociationService:
    """Wire the association service from Settings (uncached constructor).

    Same no-key gate as chat and summarize: without `CHAT_API_KEY` this
    raises `ChatUnavailableError` before any document load, candidate query,
    or model call. Reuses chat's error (503 `chat_unavailable`) for the same
    one-no-LLM-fallback-gate reason as summarize.
    """
    if not settings.CHAT_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Associations are not configured: set CHAT_API_KEY to enable it",
        )
    return AssociationService(
        get_chat_model(settings, effective_chat_model(settings)),
        effective_chat_model(settings),
        session_factory=SessionFactory,
        cache=get_cache(),
        cache_ttl_seconds=settings.CACHE_ASSOCIATION_TTL_S,
    )


@lru_cache(maxsize=1)
def get_association_service() -> AssociationService:
    """Cached accessor like `get_summarize_service`: one model and SDK client
    per process, own session per run, and the no-key path still fails every
    request."""
    return build_association_service(get_settings())


AssociationServiceDep = Annotated[AssociationService, Depends(get_association_service)]


def build_writing_service(settings: Settings) -> WritingService:
    """Wire the writing service from Settings (uncached constructor).

    Same no-key gate as chat: without `CHAT_API_KEY` this raises
    `ChatUnavailableError` BEFORE any stream can start (a clean 503 envelope,
    never a half-open stream). Unlike chat's constructor, the retriever may
    legitimately degrade to BM25 when no embedding key is configured —
    retrieval is optional for writing, so such runs simply report
    `mode: "bm25"`. Wrapped MCP tools are not wired into writing yet; the
    agent's `extra_tools` seam stays empty until that later task.

    Ordering note: FastAPI resolves dependencies before body validation, so
    an unconfigured deployment answers 503 `chat_unavailable` even for
    invalid request bodies (see `build_chat_service`).
    """
    if not settings.CHAT_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Writing assistance is not configured: set CHAT_API_KEY to enable it",
        )
    provider = embedding_provider_from_settings(settings)
    if provider is None:
        logger.warning("vector_search_disabled", reason="embedding_api_key_not_configured")
    return WritingService(
        _build_retriever(settings, provider),
        get_chat_model(settings, effective_chat_model(settings)),
        mode="hybrid" if provider is not None else "bm25",
    )


@lru_cache(maxsize=1)
def get_writing_service() -> WritingService:
    """Cached accessor like `get_chat_service`: one model, one retriever and
    one SDK client per process; `lru_cache` never memoizes raised exceptions,
    so the no-key path still fails every request."""
    return build_writing_service(get_settings())


WritingServiceDep = Annotated[WritingService, Depends(get_writing_service)]


def get_agent_operation_service(
    session: SessionDep, background_tasks: BackgroundTasks
) -> AgentOperationService:
    """One service per request, sharing the request's session.

    Unlike chat/summarize/associations there is NO constructor gate: creating,
    inspecting, resuming, and applying operations are non-LLM workflows that
    must work with or without an API key. Only the structured draft run needs
    the model/retriever, and that is wired here when CHAT_API_KEY is present —
    an unconfigured deployment keeps every non-LLM operation endpoint and 503s
    only `POST /operations/draft` at call time (`ChatUnavailableError`).
    """
    settings = get_settings()
    model = (
        get_chat_model(settings, effective_chat_model(settings))
        if settings.CHAT_API_KEY.get_secret_value()
        else None
    )
    retriever = (
        _build_retriever(settings, embedding_provider_from_settings(settings)) if model else None
    )
    return AgentOperationService(
        session,
        enqueuer=make_index_enqueuer(background_tasks),
        model=model,
        retriever=retriever,
    )


AgentOperationServiceDep = Annotated[AgentOperationService, Depends(get_agent_operation_service)]
