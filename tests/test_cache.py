"""Cache primitive: NullCache semantics, RedisCache best-effort degradation,
key versioning, and the disabled-mode wiring (all offline, zero Redis)."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

import app.api.deps as deps
from app.core.cache import (
    CACHE_KEY_VERSION,
    SEARCH_EPOCH_KEY,
    NullCache,
    RedisCache,
    cache_key,
)


class FakeRedisClient:
    """redis.asyncio.Redis stand-in; `fail` makes every command raise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.fail = fail
        self.ex_values: list[int | None] = []

    async def get(self, key: str) -> bytes | None:
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: bytes | str, *, ex: int | None = None) -> object:
        if self.fail:
            raise ConnectionError("redis down")
        self.ex_values.append(ex)
        self.store[key] = bytes(value)
        return True

    async def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        current = int(self.store.get(key, b"0")) + 1
        self.store[key] = str(current).encode()
        return current

    async def aclose(self) -> object:
        return True


async def test_null_cache_get_misses_and_set_drops():
    cache = NullCache()
    await cache.set("k", b"v", ttl_seconds=10)
    assert await cache.get("k") is None


async def test_null_cache_incr_counts_in_process():
    cache = NullCache()
    assert await cache.incr(SEARCH_EPOCH_KEY) == 1
    assert await cache.incr(SEARCH_EPOCH_KEY) == 2


async def test_cache_key_carries_version_prefix():
    assert cache_key("emb", "m", 1536, "abc").startswith(f"kb:{CACHE_KEY_VERSION}:emb:")
    assert f"kb:{CACHE_KEY_VERSION}:search:epoch" == SEARCH_EPOCH_KEY


async def test_redis_cache_round_trips_bytes_and_passes_ttl():
    client = FakeRedisClient()
    cache = RedisCache(client)
    await cache.set(cache_key("emb", "a"), b"\x00\x01", ttl_seconds=30)
    assert client.ex_values[-1] == 30
    assert await cache.get(cache_key("emb", "a")) == b"\x00\x01"


async def test_redis_cache_ttl_zero_sets_no_expiry():
    client = FakeRedisClient()
    cache = RedisCache(client)
    await cache.set(cache_key("summary", "a"), b"x", ttl_seconds=0)
    assert client.ex_values[-1] is None


async def test_redis_cache_fault_on_get_degrades_to_miss_with_warning():
    cache = RedisCache(FakeRedisClient(fail=True))
    with capture_logs() as logs:
        assert await cache.get(cache_key("emb", "a")) is None
    errors = [e for e in logs if e["event"] == "cache_error"]
    assert len(errors) == 1
    assert errors[0]["op"] == "get"
    assert errors[0]["domain"] == "emb"
    assert errors[0]["error_class"] == "ConnectionError"
    # No key contents in logs.
    assert all("kb:c1" not in str(e) for e in logs)


async def test_redis_cache_fault_on_set_and_incr_degrade():
    cache = RedisCache(FakeRedisClient(fail=True))
    with capture_logs() as logs:
        await cache.set(cache_key("summary", "a"), b"v", ttl_seconds=1)
        assert await cache.incr(SEARCH_EPOCH_KEY) == -1  # sentinel, never raises
    ops = {e["op"] for e in logs if e["event"] == "cache_error"}
    assert ops == {"set", "incr"}


async def test_redis_cache_incr_counts():
    client = FakeRedisClient()
    cache = RedisCache(client)
    assert await cache.incr(SEARCH_EPOCH_KEY) == 1
    assert await cache.incr(SEARCH_EPOCH_KEY) == 2


@pytest.fixture
def disabled_cache_env(monkeypatch):
    """Pin Settings to disabled-cache variants and reset the deps global."""
    deps._shared_cache = None
    yield monkeypatch
    deps._shared_cache = None


def _settings_with(monkeypatch, **fields: str) -> None:
    from app.core.config import Settings

    settings = Settings(_env_file=None, **fields)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)


def test_get_cache_returns_null_cache_when_redis_url_empty(disabled_cache_env):
    _settings_with(disabled_cache_env)
    assert isinstance(deps.get_cache(), NullCache)


def test_get_cache_returns_null_cache_when_cache_enabled_false(disabled_cache_env):
    _settings_with(disabled_cache_env, REDIS_URL="redis://localhost:6379/0", CACHE_ENABLED=False)
    assert isinstance(deps.get_cache(), NullCache)


def test_get_cache_builds_redis_cache_when_enabled(disabled_cache_env):
    _settings_with(disabled_cache_env, REDIS_URL="redis://localhost:6379/0")
    cache = deps.get_cache()
    assert isinstance(cache, RedisCache)
    # Process-lifetime: the same handle comes back.
    assert deps.get_cache() is cache


async def test_close_cache_resets_the_shared_handle(disabled_cache_env):
    _settings_with(disabled_cache_env)
    deps.get_cache()
    await deps.close_cache()
    assert deps._shared_cache is None
