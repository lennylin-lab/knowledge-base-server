"""Best-effort Redis cache primitive — cross-cutting infra like `core/database.py`.

The cache is a pure performance optimization: correctness never depends on
it. `RedisCache` wraps redis-py asyncio and degrades on ANY redis/OSError
fault (get -> miss, set -> drop, incr -> sentinel) instead of raising — a
cache problem can never turn a working request into a 5xx or a broken
stream (the `index_enqueue_failed` / vector-search-degradation philosophy).
The redis import is confined to this module (the `arq`-in-`rag/worker.py`
infra-import convention); every other layer only ever sees the `Cache`
Protocol, `NullCache`, and key-building helpers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)

# Schema version of the cached value formats: bumping it invalidates the
# whole cache (keys are versioned, values are never migrated in place).
CACHE_KEY_VERSION = "c1"
KEY_PREFIX = f"kb:{CACHE_KEY_VERSION}"

# The global search-epoch counter key: every document write increments it,
# so the whole search-result cache is invalidated by any corpus change.
SEARCH_EPOCH_KEY = f"{KEY_PREFIX}:search:epoch"


def cache_key(domain: str, *parts: object) -> str:
    """Build a versioned key: `kb:c1:<domain>:<part>:...`.

    Parts are stringified by the caller's domain (ids, hashes, ints); `None`
    parts are the caller's responsibility to map (e.g. `"-"` for no tag).
    """
    return ":".join([KEY_PREFIX, domain, *(str(part) for part in parts)])


@runtime_checkable
class Cache(Protocol):
    """Minimal async byte cache; every operation is best-effort by contract."""

    async def get(self, key: str) -> bytes | None: ...

    async def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None: ...

    async def incr(self, key: str) -> int: ...

    async def aclose(self) -> None: ...


class NullCache:
    """Disabled-mode no-op: every get misses, every set drops.

    `incr` counts in-process only so the search-epoch seam stays callable
    with caching off (nothing ever reads the epoch in that mode — the
    retriever also runs cache-less — but DocumentService's bump path stays
    uniform and harmless).
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return None

    async def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        return None

    async def incr(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def aclose(self) -> None:
        return None


def _domain_of(key: str) -> str:
    """Parse the domain segment (`kb:c1:<domain>:...`) for logging — keys
    themselves are never logged (they embed content hashes, not secrets, but
    the discipline is: counts and classes only)."""
    parts = key.split(":", 3)
    versioned = len(parts) >= 3 and parts[0] == "kb" and parts[1] == CACHE_KEY_VERSION
    return parts[2] if versioned else "unknown"


class RedisCache:
    """redis-py asyncio wrapper: every op is try/except -> `cache_error`
    warning (op, domain, error_class — never the key) -> degrade. Never
    raises. `ttl_seconds <= 0` means no expiry (persist until a version
    bump or eviction)."""

    def __init__(self, client: _RedisAsyncClient) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> RedisCache:
        """Build from a `redis://` DSN. The client connects lazily — nothing
        touches the network until the first command."""
        import redis.asyncio as redis_asyncio

        # redis-py ships no type hints; the module override in pyproject
        # marks it Any, so this call stays unchecked (flagged once here).
        client: _RedisAsyncClient = redis_asyncio.from_url(url)  # type: ignore[no-untyped-call]
        return cls(client)

    async def get(self, key: str) -> bytes | None:
        try:
            value = await self._client.get(key)
        except Exception as exc:
            self._warn("get", key, exc)
            return None
        if isinstance(value, str):
            return value.encode("utf-8")
        return value

    async def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        try:
            await self._client.set(key, value, ex=ttl_seconds if ttl_seconds > 0 else None)
        except Exception as exc:
            self._warn("set", key, exc)

    async def incr(self, key: str) -> int:
        try:
            return int(await self._client.incr(key))
        except Exception as exc:
            self._warn("incr", key, exc)
            return -1  # sentinel: callers treat any non-positive epoch as a forced miss

    async def aclose(self) -> None:
        await self._client.aclose()

    def _warn(self, op: str, key: str, exc: Exception) -> None:
        logger.warning(
            "cache_error",
            domain=_domain_of(key),
            op=op,
            error_class=type(exc).__name__,
        )


# Structural surface of redis.asyncio.Redis this wrapper uses; declared so
# the wrapper stays type-checkable without importing redis outside tests.
class _RedisAsyncClient(Protocol):
    async def get(self, key: str) -> bytes | str | None: ...

    async def set(
        self,
        key: str,
        value: bytes | str | int | float,
        *,
        ex: int | None = ...,
    ) -> object: ...

    async def incr(self, key: str) -> int: ...

    async def aclose(self) -> object: ...
