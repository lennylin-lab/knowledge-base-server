"""Gateway model-discovery surface (model control plane, v1.3).

One module talks to the gateway's discovery endpoints so `llm/` stays the
ONLY provider-SDK layer. Everything here is bounded-timeout and
failure-tolerant: discovery problems degrade to env fallbacks, never to
unbounded calls or raised secrets/shape details.

- `discover()` reads `capabilities` (`embedding_dim`) and
  `retrieval_profile` off `GET /v1/models/{model}` (pydantic `extra="allow"`
  resource — attribute access, defensively type-checked).
- When `CHAT_MODEL` is unset, one minimal chat probe WITHOUT `model` asks
  the gateway to backfill its configured default; the response's `model`
  echo is the resolved name. The raw SDK call (not pydantic-ai) is used
  because pydantic-ai requires a concrete model name at construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx2
import openai
import structlog
from openai import AsyncOpenAI

from app.core.config import Settings

logger = structlog.get_logger(__name__)

# Startup-only traffic: bounded so a down gateway cannot stall boot for long
# (the SDK client itself gets no retries here — the probe does its single
# explicit retry below). The budget must absorb real vendors whose first
# token takes >10s, hence 30s rather than a tight local-network bound.
_DISCOVERY_TIMEOUT_S = 30.0
_PROBE_ATTEMPTS = 2


@dataclass(frozen=True)
class ModelFacts:
    """What the gateway told us about the effective chat model.

    `retrieval_profile` is opaque here — key validation/casting lives in
    `llm/profile.py` (server-side, per the integration guide).
    """

    model_name: str
    embedding_dim: int | None
    retrieval_profile: Mapping[str, object] = field(default_factory=dict)


def _build_client(
    settings: Settings, *, http_client: httpx2.AsyncClient | None = None
) -> AsyncOpenAI:
    """Raw client for discovery only; bounded timeout, no SDK retries.

    When `http_client` is injected (tests: MockTransport), the SDK uses its
    transport as-is and ignores the timeout argument.
    """
    return AsyncOpenAI(
        base_url=settings.CHAT_BASE_URL,
        api_key=settings.CHAT_API_KEY.get_secret_value(),
        timeout=_DISCOVERY_TIMEOUT_S,
        max_retries=0,
        http_client=http_client,
        default_headers={"User-Agent": "knowledge-base-server"},
    )


async def discover(settings: Settings, *, client: AsyncOpenAI | None = None) -> ModelFacts | None:
    """Resolve the effective model facts, or `None` when the gateway cannot
    answer.

    Failure policy (design §1): a failed chat-default probe (only needed
    when `CHAT_MODEL` is unset) returns `None` — the caller may fail startup.
    A failed `models.retrieve` still returns the (probe- or env-resolved)
    name with dim/profile fallbacks so env-configured deployments boot.
    """
    own_client = client is None
    client = client or _build_client(settings)
    try:
        # `KB_CHAT_MODEL=` (shell "unset") parses as "" — and a whitespace-only
        # value is just as unusable: both take the discovery-probe path.
        name = settings.CHAT_MODEL.strip() if settings.CHAT_MODEL else None
        if not name:
            name = await _probe_default_model(client)
            if name is None:
                return None
        facts = ModelFacts(model_name=name, embedding_dim=None, retrieval_profile={})
        try:
            resource = await client.models.retrieve(name)
        except Exception as exc:  # any gateway problem is a fallback, not a crash
            logger.warning(
                "model_discovery_failed",
                model=name,
                error_class=type(exc).__name__,
            )
            return facts
        return _facts_from_resource(facts.model_name, resource)
    finally:
        if own_client:
            await client.close()


def _facts_from_resource(model_name: str, resource: object) -> ModelFacts:
    """Defensive parse of the `extra="allow"` Model resource: unexpected
    shapes degrade to None/{} (env fallbacks), never raise."""
    dim: int | None = None
    capabilities = getattr(resource, "capabilities", None)
    if isinstance(capabilities, Mapping):
        raw_dim = capabilities.get("embedding_dim")
        if isinstance(raw_dim, int) and not isinstance(raw_dim, bool) and raw_dim > 0:
            dim = raw_dim
        elif raw_dim is not None:
            logger.warning(
                "model_discovery_dim_unusable",
                model=model_name,
                value_type=type(raw_dim).__name__,
            )
    profile_raw = getattr(resource, "retrieval_profile", None)
    profile: Mapping[str, object] = dict(profile_raw) if isinstance(profile_raw, Mapping) else {}
    return ModelFacts(
        model_name=model_name,
        embedding_dim=dim,
        retrieval_profile=profile,
    )


async def _probe_default_model(client: AsyncOpenAI) -> str | None:
    """One minimal chat request WITHOUT `model`; the gateway backfills its
    configured default and echoes it in the response's `model` field.
    One retry; any failure ⇒ None (identifiers only in logs)."""
    body: dict[str, object] = {
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    for attempt in range(_PROBE_ATTEMPTS):
        try:
            response = await client.post("/chat/completions", cast_to=object, body=body)
        except (openai.APIError, httpx2.HTTPError) as exc:
            logger.warning(
                "chat_default_probe_failed",
                attempt=attempt + 1,
                error_class=type(exc).__name__,
            )
            continue
        if isinstance(response, Mapping):
            model = response.get("model")
            if isinstance(model, str) and model:
                return model
        logger.warning(
            "chat_default_probe_unexpected_shape",
            attempt=attempt + 1,
            response_type=type(response).__name__,
        )
        break
    return None
