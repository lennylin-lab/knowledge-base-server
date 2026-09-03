"""Embedding provider: protocol shape, fake determinism, SDK error mapping.

The OpenAI client is stubbed at the llm/ boundary (per quality-guidelines.md);
the real provider is only touched under the `live_llm` marker.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest
from openai import Omit

from app.core.config import Settings
from app.core.exceptions import LLMProviderError, LLMRateLimitedError
from app.llm.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider
from app.llm.models import get_chat_model
from fakes import FakeEmbeddingProvider, hermetic_settings


def _status_error(exc_type: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://provider.test/embeddings")
    response = httpx.Response(status_code=status, request=request)
    return exc_type(f"{status}", response=response, body=None)


class StubEmbeddings:
    """Minimal `client.embeddings` namespace: records calls, replays a result."""

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def create(
        self,
        *,
        model: str,
        input: list[str],
        encoding_format: str = "float",
        dimensions: object = None,
    ) -> object:
        self.calls.append(
            {
                "model": model,
                "input": input,
                "encoding_format": encoding_format,
                "dimensions": dimensions,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


def _response(vectors: list[list[float]]) -> SimpleNamespace:
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=vector) for vector in vectors],
        usage=SimpleNamespace(total_tokens=12),
    )


def make_provider(stub: StubEmbeddings) -> OpenAIEmbeddingProvider:
    client = SimpleNamespace(embeddings=stub)
    return OpenAIEmbeddingProvider(
        base_url="http://provider.test/v1", api_key="test-key", model="embed-x", client=client
    )


# --- protocol + fake (shared offline stand-in) ---


def test_fake_provider_satisfies_the_protocol():
    assert isinstance(FakeEmbeddingProvider(), EmbeddingProvider)
    assert isinstance(make_provider(StubEmbeddings()), EmbeddingProvider)


async def test_fake_provider_is_deterministic_and_normalized():
    fake = FakeEmbeddingProvider(dim=8)

    first = await fake.embed_texts(["alpha", "beta"])
    second = await fake.embed_texts(["alpha"])

    assert first[0] == second[0]
    assert all(len(vector) == 8 for vector in first)
    assert all(abs(sum(c * c for c in vector) - 1.0) < 1e-9 for vector in first)
    assert fake.calls == [["alpha", "beta"], ["alpha"]]


async def test_fake_provider_raises_configured_error():
    fake = FakeEmbeddingProvider()
    fake.error = LLMProviderError("boom")

    with pytest.raises(LLMProviderError):
        await fake.embed_texts(["alpha"])


# --- OpenAI-compatible provider against a stubbed SDK client ---


async def test_provider_returns_vectors_in_input_order():
    stub = StubEmbeddings(result=_response([[0.1], [0.2], [0.3]]))
    provider = make_provider(stub)

    vectors = await provider.embed_texts(["a", "b", "c"])

    assert vectors == [[0.1], [0.2], [0.3]]
    call = stub.calls[0]
    assert call["model"] == "embed-x"
    assert call["input"] == ["a", "b", "c"]
    # float must be explicit (SDK default base64 breaks OpenRouter/Nvidia);
    # dimensions stays omitted when no width is configured.
    assert call["encoding_format"] == "float"
    assert isinstance(call["dimensions"], Omit)


async def test_provider_empty_batch_skips_the_client():
    stub = StubEmbeddings()
    provider = make_provider(stub)

    assert await provider.embed_texts([]) == []
    assert stub.calls == []


async def test_provider_maps_rate_limit_error():
    stub = StubEmbeddings(error=_status_error(openai.RateLimitError, 429))
    provider = make_provider(stub)

    with pytest.raises(LLMRateLimitedError):
        await provider.embed_texts(["a"])


async def test_provider_maps_provider_status_error():
    for status in (500, 503):
        stub = StubEmbeddings(error=_status_error(openai.APIStatusError, status))
        provider = make_provider(stub)

        with pytest.raises(LLMProviderError):
            await provider.embed_texts(["a"])


async def test_provider_maps_connection_error():
    request = httpx.Request("POST", "http://provider.test/embeddings")
    stub = StubEmbeddings(error=openai.APIConnectionError(message="down", request=request))
    provider = make_provider(stub)

    with pytest.raises(LLMProviderError):
        await provider.embed_texts(["a"])


async def test_provider_rejects_mismatched_vector_count():
    stub = StubEmbeddings(result=_response([[0.1]]))
    provider = make_provider(stub)

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.embed_texts(["a", "b"])

    assert exc_info.value.details == {"expected": 2, "received": 1}


async def test_provider_passes_configured_dimensions():
    stub = StubEmbeddings(result=_response([[0.1, 0.2]]))
    provider = OpenAIEmbeddingProvider(
        base_url="http://provider.test/v1",
        api_key="test-key",
        model="embed-x",
        dimensions=2,
        client=SimpleNamespace(embeddings=stub),
    )

    await provider.embed_texts(["a"])

    assert stub.calls[0]["dimensions"] == 2


async def test_provider_rejects_unexpected_vector_dimension():
    stub = StubEmbeddings(result=_response([[0.1, 0.2, 0.3]]))
    provider = OpenAIEmbeddingProvider(
        base_url="http://provider.test/v1",
        api_key="test-key",
        model="embed-x",
        dimensions=2,
        client=SimpleNamespace(embeddings=stub),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.embed_texts(["a"])

    assert exc_info.value.details == {"expected": 2, "received": 3}


def test_from_settings_wires_client_configuration():
    settings = hermetic_settings(
        EMBEDDING_BASE_URL="http://provider.test/v1",
        EMBEDDING_API_KEY="sk-test",
        EMBEDDING_MODEL="embed-x",
    )

    provider = OpenAIEmbeddingProvider.from_settings(settings)

    assert provider._model == "embed-x"
    assert provider._dimensions == 1536


def test_layers_read_their_own_provider_variables():
    # Pin the config isolation: the embedding leg is built from EMBEDDING_*,
    # the chat leg from CHAT_* — changing one side never touches the other.
    settings = hermetic_settings(
        EMBEDDING_BASE_URL="http://embed.test/v1",
        EMBEDDING_API_KEY="embed-key",
        EMBEDDING_MODEL="embed-x",
        CHAT_BASE_URL="http://chat.test/v1",
        CHAT_API_KEY="chat-key",
    )

    provider = OpenAIEmbeddingProvider.from_settings(settings)
    chat_model = get_chat_model(settings)

    assert str(provider._client.base_url).rstrip("/") == "http://embed.test/v1"
    assert provider._client.api_key == "embed-key"
    assert str(chat_model.provider.client.base_url).rstrip("/") == "http://chat.test/v1"
    assert chat_model.provider.client.api_key == "chat-key"


class StubSdkClient:
    """Client shell that records close() (provider owns its lifetime)."""

    def __init__(self, stub: StubEmbeddings) -> None:
        self.embeddings = stub
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_provider_aclose_closes_the_sdk_client():
    client = StubSdkClient(StubEmbeddings())
    provider = OpenAIEmbeddingProvider(
        base_url="http://provider.test/v1", api_key="k", model="m", client=client
    )

    await provider.aclose()

    assert client.closed is True


@pytest.mark.live_llm
async def test_live_provider_returns_configured_dimension_vectors():
    provider = OpenAIEmbeddingProvider.from_settings(Settings())
    vectors = await provider.embed_texts(["hello world"])

    assert len(vectors) == 1
    assert all(len(vector) == 1536 for vector in vectors)
