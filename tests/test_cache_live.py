"""Live Redis smoke (deselected by default): one real round trip per domain.

Run manually with a reachable Redis:
    uv run pytest -m live_redis tests/test_cache_live.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.core.cache import SEARCH_EPOCH_KEY, RedisCache, cache_key

pytestmark = pytest.mark.live_redis


@pytest.fixture
def cache() -> RedisCache:
    url = os.environ.get("KB_REDIS_URL", "")
    if not url:
        pytest.skip("KB_REDIS_URL not configured")
    cache = RedisCache.from_url(url)
    yield cache
    asyncio.run(cache.aclose())


async def test_live_cache_round_trip_and_epoch(cache: RedisCache):
    key = cache_key("emb", "smoke", "v")
    await cache.set(key, b"\x01\x02", ttl_seconds=30)
    assert await cache.get(key) == b"\x01\x02"

    first = await cache.incr(SEARCH_EPOCH_KEY)
    assert await cache.incr(SEARCH_EPOCH_KEY) == first + 1
