"""Unit tests for the in-house UUIDv7 generator (utils/ids.py)."""

from __future__ import annotations

import time
from itertools import pairwise
from uuid import RFC_4122, UUID

from app.utils.ids import uuid7


def test_uuid7_layout_matches_rfc9562() -> None:
    for value in (uuid7(), uuid7()):
        assert isinstance(value, UUID)
        assert len(value.hex) == 32
        assert value.version == 7
        assert value.variant == RFC_4122


def test_uuid7_timestamp_prefix_is_current_time() -> None:
    before_ms = time.time_ns() // 1_000_000
    value = uuid7()
    after_ms = time.time_ns() // 1_000_000
    # Top 48 bits are unix milliseconds (allow the 1ms overflow bump).
    assert before_ms - 1 <= value.int >> 80 <= after_ms + 1


def test_uuid7_is_strictly_increasing_within_process() -> None:
    # 10k ids span at most a few milliseconds, so this exercises the
    # same-millisecond counter path, not just the timestamp prefix.
    ids = [uuid7() for _ in range(10_000)]
    assert all(a.int < b.int for a, b in pairwise(ids))


def test_uuid7_no_duplicates() -> None:
    ids = {uuid7() for _ in range(10_000)}
    assert len(ids) == 10_000
