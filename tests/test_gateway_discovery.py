"""Gateway v1.3 model-discovery tests (fully offline, MockTransport).

Covers `llm/discovery.py` (retrieve parsing, chat-default probe, failure
policy), the startup resolution in `api/deps.py` (`resolve_model_control_plane`:
unset-model failure, discovered dim/profile threading, source logging), and
`llm/profile.py` (`apply_profile` precedence: profile > env > Retriever
defaults; unknown keys ignored; invalid values tolerated).
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

import app.api.deps as deps_module
from app.api.deps import (
    _build_retriever,
    effective_chat_model,
    effective_embedding_dim,
    embedding_provider_from_settings,
    reset_model_control_plane,
    resolve_model_control_plane,
)
from app.llm.discovery import discover
from app.llm.embeddings import CachingEmbeddingProvider, OpenAIEmbeddingProvider
from app.llm.profile import RETRIEVER_THRESHOLD_KEYS, apply_profile
from fakes import hermetic_settings

MODEL = "gpt-5.5"
CHAT_KEY = SecretStr("test-key")


@pytest.fixture(autouse=True)
def _clean_control_plane():
    """Every test starts and ends with no startup-resolved state."""
    reset_model_control_plane()
    yield
    reset_model_control_plane()


def mock_client(handler: Any) -> Any:
    """AsyncOpenAI test client over an httpx2 MockTransport (no network)."""
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        base_url="http://gateway.test/v1",
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


def model_resource(*, dim: int | None = 1536, profile: dict | None = None) -> dict:
    """`GET /v1/models/{model}` payload (extra fields ride through)."""
    body: dict[str, Any] = {"id": MODEL, "object": "model", "owned_by": "gateway"}
    body["capabilities"] = {} if dim is None else {"embeddings": True, "embedding_dim": dim}
    if profile is not None:
        body["retrieval_profile"] = profile
    return body


def chat_completion(model_echo: str) -> dict:
    """Minimal chat-completions payload: the `model` echo is the point."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model_echo,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}],
    }


def gateway_handler(*, dim: int, profile: dict | None = None) -> Any:
    """A full fake gateway: probe POST answers the default echo; models
    retrieve returns the capabilities/profile payload."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(200, json=chat_completion(MODEL))
        return httpx2.Response(200, json=model_resource(dim=dim, profile=profile))

    return handler


# --- discovery: retrieve parsing ---


async def test_discovery_parses_capabilities_and_profile_with_env_model():
    settings = hermetic_settings(CHAT_MODEL=MODEL, CHAT_API_KEY=CHAT_KEY)
    client = mock_client(
        lambda request: httpx2.Response(
            200,
            json=model_resource(
                dim=1536,
                profile={
                    "search_vector_max_distance": "0.5",
                    "unknown_key": 1,
                },
            ),
        )
    )

    facts = await discover(settings, client=client)

    assert facts is not None
    assert facts.model_name == MODEL
    assert facts.embedding_dim == 1536
    assert facts.retrieval_profile == {"search_vector_max_distance": "0.5", "unknown_key": 1}


async def test_discovery_without_dim_or_profile_yields_fallbacks():
    settings = hermetic_settings(CHAT_MODEL=MODEL, CHAT_API_KEY=CHAT_KEY)
    client = mock_client(lambda request: httpx2.Response(200, json=model_resource(dim=None)))

    facts = await discover(settings, client=client)

    assert facts is not None
    assert facts.embedding_dim is None
    assert facts.retrieval_profile == {}


async def test_discovery_retrieve_failure_keeps_env_name():
    settings = hermetic_settings(CHAT_MODEL=MODEL, CHAT_API_KEY=CHAT_KEY)

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("gateway down")

    facts = await discover(settings, client=mock_client(handler))

    # Env model set: the name is known without the gateway; facts degrade.
    assert facts == discover_fallback(MODEL)


def discover_fallback(name: str) -> Any:
    from app.llm.discovery import ModelFacts

    return ModelFacts(model_name=name, embedding_dim=None, retrieval_profile={})


# --- discovery: chat-default probe (CHAT_MODEL unset) ---


async def test_discovery_probe_resolves_backfilled_default():
    settings = hermetic_settings(CHAT_API_KEY=CHAT_KEY)  # CHAT_MODEL unset
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.method == "POST":
            # The gateway backfills the default and echoes it.
            body = request.read()
            assert b'"model"' not in body  # probe carries NO model
            return httpx2.Response(200, json=chat_completion(MODEL))
        return httpx2.Response(200, json=model_resource(dim=256))

    facts = await discover(settings, client=mock_client(handler))

    assert facts is not None
    assert facts.model_name == MODEL
    assert facts.embedding_dim == 256
    assert [r.method for r in requests] == ["POST", "GET"]


async def test_discovery_probe_connection_failure_returns_none():
    settings = hermetic_settings(CHAT_API_KEY=CHAT_KEY)

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("gateway down")

    assert await discover(settings, client=mock_client(handler)) is None


async def test_discovery_probe_timeout_returns_none():
    settings = hermetic_settings(CHAT_API_KEY=CHAT_KEY)

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("gateway stalled")

    assert await discover(settings, client=mock_client(handler)) is None


async def test_discovery_probe_unexpected_shape_returns_none():
    settings = hermetic_settings(CHAT_API_KEY=CHAT_KEY)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"object": "chat.completion"})  # no model echo

    assert await discover(settings, client=mock_client(handler)) is None


# --- startup resolution (deps.resolve_model_control_plane) ---


async def test_resolver_unset_model_and_failed_discovery_fails_startup():
    settings = hermetic_settings(CHAT_API_KEY=CHAT_KEY)  # CHAT_MODEL unset

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("gateway down")

    with pytest.raises(RuntimeError, match="KB_CHAT_MODEL"):
        await resolve_model_control_plane(settings, client=mock_client(handler))


async def test_resolver_empty_string_model_resolves_default():
    # `KB_CHAT_MODEL=` is the common shell "unset": pydantic parses it as ""
    # — it must behave exactly like unset (probe path, discovered default).
    settings = hermetic_settings(CHAT_MODEL="", CHAT_API_KEY=CHAT_KEY)

    with capture_logs() as logs:
        await resolve_model_control_plane(settings, client=mock_client(gateway_handler(dim=1536)))

    assert effective_chat_model(settings) == MODEL
    resolved = next(log for log in logs if log["event"] == "model_control_plane_resolved")
    assert resolved["chat_model_source"] == "discovered"


async def test_resolver_whitespace_model_and_failed_discovery_fails_startup():
    # Whitespace-only is just as unusable as empty: treated as unset, so a
    # failed discovery fails startup instead of sending a blank model name.
    settings = hermetic_settings(CHAT_MODEL="   ", CHAT_API_KEY=CHAT_KEY)

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("gateway down")

    with pytest.raises(RuntimeError, match="KB_CHAT_MODEL"):
        await resolve_model_control_plane(settings, client=mock_client(handler))


async def test_resolver_unset_model_discovers_default_and_threads_it():
    settings = hermetic_settings(CHAT_API_KEY=CHAT_KEY)
    client = mock_client(gateway_handler(dim=256))

    with capture_logs() as logs:
        await resolve_model_control_plane(settings, client=client)

    assert effective_chat_model(settings) == MODEL
    assert effective_embedding_dim(settings) == 256
    resolved = next(log for log in logs if log["event"] == "model_control_plane_resolved")
    assert resolved["chat_model_source"] == "discovered"
    assert resolved["embedding_dim_source"] == "discovered"
    assert resolved["thresholds"] == {key: "env" for key in RETRIEVER_THRESHOLD_KEYS}


async def test_resolver_env_model_unchanged_and_sources_reported():
    settings = hermetic_settings(CHAT_MODEL=MODEL, CHAT_API_KEY=CHAT_KEY)
    client = mock_client(
        lambda request: httpx2.Response(
            200,
            json=model_resource(
                dim=1536,
                profile={
                    "search_vector_max_distance": 0.3,
                },
            ),
        )
    )

    with capture_logs() as logs:
        await resolve_model_control_plane(settings, client=client)

    # Env model wins even though the gateway answered.
    assert effective_chat_model(settings) == MODEL
    assert effective_embedding_dim(settings) == 1536
    resolved = next(log for log in logs if log["event"] == "model_control_plane_resolved")
    assert resolved["chat_model_source"] == "env"
    assert resolved["embedding_dim_source"] == "discovered"
    assert resolved["thresholds"]["vector_max_distance"] == "profile"


async def test_resolver_warns_on_dim_mismatch_with_pgvector_column():
    settings = hermetic_settings(CHAT_MODEL=MODEL, CHAT_API_KEY=CHAT_KEY)
    client = mock_client(lambda request: httpx2.Response(200, json=model_resource(dim=256)))

    with capture_logs() as logs:
        await resolve_model_control_plane(settings, client=client)

    mismatch = next(log for log in logs if log["event"] == "embedding_dim_mismatch")
    assert mismatch["effective_dim"] == 256
    assert mismatch["pgvector_column_width"] == 1536


async def test_resolver_without_chat_key_skips_the_gateway():
    settings = hermetic_settings()  # no key, no model: chat is 503 anyway

    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover
        raise AssertionError("no gateway traffic without a chat key")

    with capture_logs() as logs:
        await resolve_model_control_plane(settings, client=mock_client(handler))

    resolved = next(log for log in logs if log["event"] == "model_control_plane_resolved")
    assert resolved["chat_model_source"] == "unconfigured"


# --- discovered dim feeds the embedding provider ---


def test_discovered_dim_feeds_provider_width_and_cache_key():
    settings = hermetic_settings(
        EMBEDDING_BASE_URL="http://embed.test/v1",
        EMBEDDING_API_KEY="embed-key",
        EMBEDDING_MODEL="embed-x",
        CACHE_ENABLED=True,
        REDIS_URL="redis://localhost:6379",
    )
    deps_module._resolved_embedding_dim = 256

    provider = embedding_provider_from_settings(settings)

    assert isinstance(provider, CachingEmbeddingProvider)
    raw = provider._key("hello")
    assert "256" in raw and "1536" not in raw
    inner = provider._inner
    assert isinstance(inner, OpenAIEmbeddingProvider)
    assert inner._dimensions == 256


def test_env_dim_used_without_resolution():
    settings = hermetic_settings(EMBEDDING_API_KEY="embed-key", EMBEDDING_DIM=1536)

    provider = embedding_provider_from_settings(settings)

    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider._dimensions == 1536


# --- retrieval profile override (Stage 3) ---


def test_apply_profile_overrides_env_values():
    base = {"vector_max_distance": 0.45, "bm25_min_coverage": "70%"}
    merged = apply_profile(
        base, {"search_vector_max_distance": 0.3, "search_bm25_min_coverage": "50%"}
    )

    assert merged["vector_max_distance"] == 0.3
    assert merged["bm25_min_coverage"] == "50%"


def test_apply_profile_fills_missing_kwargs_over_retriever_defaults():
    # An empty env/base mapping: the profile value lands anyway — precedence
    # profile > env > Retriever module defaults.
    merged = apply_profile({}, {"search_rrf_min_relative": "0.5"})

    assert merged["rrf_min_relative"] == 0.5


def test_apply_profile_ignores_unknown_keys():
    # Unknown keys are ignored with a debug log (not asserted here — the
    # suite's INFO level filters debug records out of capture_logs).
    merged = apply_profile({"vector_max_distance": 0.45}, {"bogus": 1})

    assert merged == {"vector_max_distance": 0.45}


def test_apply_profile_tolerates_invalid_values_keeping_env():
    with capture_logs() as logs:
        merged = apply_profile({"bm25_min_score": 0.0}, {"search_bm25_min_score": "not-a-number"})

    assert merged["bm25_min_score"] == 0.0
    assert any(log["event"] == "profile_value_invalid" for log in logs)


def test_profile_overrides_reach_the_retriever():
    settings = hermetic_settings(SEARCH_VECTOR_MAX_DISTANCE=0.45)
    deps_module._resolved_profile = {"search_vector_max_distance": 0.2}
    try:
        retriever = _build_retriever(settings, provider=None)
    finally:
        deps_module._resolved_profile = {}

    assert retriever._vector_max_distance == 0.2


# --- effective chat model precedence ---


def test_effective_chat_model_env_beats_resolved_beats_legacy_default():
    settings = hermetic_settings(CHAT_MODEL=MODEL)
    assert effective_chat_model(settings) == MODEL

    unset = hermetic_settings()
    assert effective_chat_model(unset) == unset.DEFAULT_CHAT_MODEL  # pre-startup fallback

    deps_module._resolved_chat_model = "discovered-model"
    try:
        assert effective_chat_model(unset) == "discovered-model"
        # Env still wins over the discovery result.
        assert effective_chat_model(settings) == MODEL
    finally:
        deps_module._resolved_chat_model = None


async def test_empty_embedding_dim_parses_to_none_and_falls_back():
    """`KB_EMBEDDING_DIM=` means discover; the pgvector width is the last resort."""
    settings = hermetic_settings(EMBEDDING_DIM="")
    assert settings.EMBEDDING_DIM is None
    deps_module.reset_model_control_plane()
    try:
        assert deps_module.effective_embedding_dim(settings) == 1536
        settings_set = hermetic_settings(EMBEDDING_DIM=768)
        assert deps_module.effective_embedding_dim(settings_set) == 768
    finally:
        deps_module.reset_model_control_plane()


async def test_discovered_dim_wins_over_empty_env_dim():
    settings = hermetic_settings(CHAT_MODEL=MODEL, CHAT_API_KEY=CHAT_KEY, EMBEDDING_DIM="")
    assert settings.EMBEDDING_DIM is None
    client = mock_client(lambda request: httpx2.Response(200, json=model_resource(dim=2560)))
    deps_module.reset_model_control_plane()
    try:
        facts = await discover(settings, client=client)
        assert facts is not None and facts.embedding_dim == 2560
    finally:
        deps_module.reset_model_control_plane()
