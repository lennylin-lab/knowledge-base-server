"""Embedding cache: per-text hits, miss-only provider calls, order
preservation, byte-exact vector round trip (offline fakes only)."""

from __future__ import annotations

from array import array

from structlog.testing import capture_logs

from app.core.cache import CACHE_KEY_VERSION, NullCache
from app.llm.embeddings import CachingEmbeddingProvider, EmbeddingProvider
from fakes import FakeCache

DIM = 8


class CountingProvider:
    """Scripted inner provider: deterministic per-text vectors, records calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[ord(text[0]) / 256] * DIM for text in texts]


def make_provider(
    inner: CountingProvider, cache: FakeCache | NullCache
) -> CachingEmbeddingProvider:
    return CachingEmbeddingProvider(inner, cache, model="test-emb-model", dim=DIM, ttl_seconds=60)


async def test_empty_batch_makes_no_calls():
    inner = CountingProvider()
    provider = make_provider(inner, FakeCache())
    assert await provider.embed_texts([]) == []
    assert inner.calls == []


async def test_first_call_is_a_full_miss_and_populates_cache():
    inner = CountingProvider()
    cache = FakeCache()
    provider = make_provider(inner, cache)
    vectors = await provider.embed_texts(["hello"])
    assert vectors == [[ord("h") / 256] * DIM]
    assert inner.calls == [["hello"]]
    assert len(cache.store) == 1
    # Key shape: kb:c1:emb:{model}:{dim}:{sha256}
    (key,) = cache.store.keys()
    assert key.startswith(f"kb:{CACHE_KEY_VERSION}:emb:test-emb-model:{DIM}:")


async def test_second_identical_call_skips_the_provider():
    inner = CountingProvider()
    cache = FakeCache()
    provider = make_provider(inner, cache)
    first = await provider.embed_texts(["hello"])
    second = await provider.embed_texts(["hello"])
    assert second == first
    assert len(inner.calls) == 1  # provider NOT invoked again


async def test_mixed_batch_calls_provider_for_misses_only_and_preserves_order():
    inner = CountingProvider()
    cache = FakeCache()
    provider = make_provider(inner, cache)
    await provider.embed_texts(["cached-text"])
    inner.calls.clear()

    result = await provider.embed_texts(["new-text", "cached-text"])
    # Input order preserved: the fresh vector first, the cached one second.
    assert result == [[ord("n") / 256] * DIM, [ord("c") / 256] * DIM]
    # Only the miss reached the provider.
    assert inner.calls == [["new-text"]]


async def test_vectors_round_trip_byte_exact():
    from app.llm.embeddings import _decode_vector, _encode_vector

    vector = [i / 7 + 1e-9 for i in range(DIM)]
    raw = _encode_vector(vector)
    assert _decode_vector(raw) == vector
    assert raw == array("d", vector).tobytes()
    # Through the full path: encode into the fake cache, then hit on round 2.
    inner = CountingProvider()
    cache = FakeCache()
    provider = CachingEmbeddingProvider(inner, cache, model="m", dim=DIM, ttl_seconds=60)
    cache.store[provider._key("hello")] = raw
    assert await provider.embed_texts(["hello"]) == [vector]


async def test_key_varies_by_model_and_dim():
    inner = CountingProvider()
    cache = FakeCache()
    a = CachingEmbeddingProvider(inner, cache, model="m1", dim=DIM, ttl_seconds=1)
    b = CachingEmbeddingProvider(inner, cache, model="m2", dim=DIM, ttl_seconds=1)
    c = CachingEmbeddingProvider(inner, cache, model="m1", dim=DIM + 1, ttl_seconds=1)
    await a.embed_texts(["t"])
    assert len(cache.store) == 1
    await b.embed_texts(["t"])
    await c.embed_texts(["t"])
    assert len(cache.store) == 3  # distinct keys per model/dim


async def test_cache_fault_on_get_degrades_to_recompute():
    inner = CountingProvider()
    cache = FakeCache(fail="get")
    provider = make_provider(inner, cache)
    with capture_logs() as logs:
        vectors = await provider.embed_texts(["hello"])
    assert len(vectors) == 1  # recompute, request still succeeds
    assert any(e["event"] == "cache_error" for e in logs)


async def test_cache_fault_on_set_still_returns_the_vector():
    inner = CountingProvider()
    cache = FakeCache(fail="set")
    provider = make_provider(inner, cache)
    vectors = await provider.embed_texts(["hello"])
    assert vectors == [[ord("h") / 256] * DIM]


async def test_hit_and_miss_logging_carries_counts_only():
    inner = CountingProvider()
    cache = FakeCache()
    provider = make_provider(inner, cache)
    await provider.embed_texts(["hello"])
    with capture_logs() as logs:
        await provider.embed_texts(["hello"])
    hits = [e for e in logs if e["event"] == "cache_hit"]
    assert hits and hits[0]["count"] == 1 and hits[0]["domain"] == "embedding"
    assert all("hello" not in str(e) for e in logs)  # text never logged


def test_caching_provider_implements_the_protocol() -> None:
    wrapper = CachingEmbeddingProvider(
        CountingProvider(), NullCache(), model="m", dim=DIM, ttl_seconds=1
    )
    assert isinstance(wrapper, EmbeddingProvider)
