"""UUIDv7 generation for entity primary keys.

UUIDv7 (RFC 9562) keeps the native Postgres `uuid` column type and every
UUID convention while making ids time-ordered: B-tree inserts stay
append-mostly instead of scattering across the whole index. The stdlib only
ships `uuid.uuid7()` from Python 3.14; the project targets 3.12, so this is
the minimal in-house implementation (48-bit unix-ms prefix, RFC "counter
method" in rand_a, random rand_b).

Trace ids (request_id / agent run_id) stay uuid4: they are not entities and
gain nothing from ordering.
"""

from __future__ import annotations

import secrets
import threading
import time
from uuid import UUID

_SEQ_BITS = 12
_SEQ_MASK = (1 << _SEQ_BITS) - 1

# Per-process monotonicity state; the lock keeps the read-modify-write of
# (_last_ms, _seq) atomic across threads.
_LOCK = threading.Lock()
_last_ms: int = 0
_seq: int = 0


def uuid7() -> UUID:
    """Time-ordered UUIDv7, strictly increasing within this process.

    The 12-bit counter makes same-millisecond ids ascend in generation
    order, so `ORDER BY id` matches insert order for ties. On counter
    overflow (4096 ids/ms) the timestamp is bumped one millisecond forward
    rather than wrapping — ids stay unique and ordered, at most 1ms early.
    """
    global _last_ms, _seq
    with _LOCK:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms = now_ms
            _seq = secrets.randbits(_SEQ_BITS)  # random start per millisecond
        else:
            _seq = (_seq + 1) & _SEQ_MASK
            if _seq == 0:  # wrapped past 4096 ids in this millisecond
                _last_ms += 1
                _seq = secrets.randbits(_SEQ_BITS)
        seq = _seq
        ms = _last_ms
    value = (
        (ms & 0xFFFF_FFFF_FFFF) << 80  # unix_ts_ms (48 bits)
        | 0x7 << 76  # version
        | seq << 64  # rand_a as the counter
        | 0b10 << 62  # variant (RFC 4122)
        | secrets.randbits(62)  # rand_b
    )
    return UUID(int=value)
