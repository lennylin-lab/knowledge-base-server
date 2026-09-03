"""Opaque keyset-cursor codecs shared by paginated list services.

Two cursor shapes live here: the (timestamp, id) pair for entities whose
sort key is a mutable column (chat sessions order by `updated_at`) and the
id-only cursor for UUIDv7 entities whose id *is* the sort key (documents
order by creation time, which v7 encodes). The payload keys stay short —
cursors travel in every list request.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

from app.core.exceptions import ValidationError

_TIMESTAMP_KEY = "ts"
_ID_KEY = "id"


def encode_cursor(timestamp: datetime, entity_id: UUID) -> str:
    """Opaque keyset cursor: urlsafe-base64 JSON of (timestamp, id)."""
    payload = json.dumps({_TIMESTAMP_KEY: timestamp.isoformat(), _ID_KEY: str(entity_id)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Inverse of `encode_cursor`; any malformed input is a 422."""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(data[_TIMESTAMP_KEY]), UUID(data[_ID_KEY])
    except (ValueError, KeyError, TypeError) as exc:
        # binascii.Error (bad base64) and JSONDecodeError subclass ValueError.
        raise ValidationError("Invalid cursor", details={"cursor": cursor}) from exc


def encode_id_cursor(entity_id: UUID) -> str:
    """Opaque id-only keyset cursor: urlsafe-base64 JSON of the id."""
    payload = json.dumps({_ID_KEY: str(entity_id)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_id_cursor(cursor: str) -> UUID:
    """Inverse of `encode_id_cursor`; any malformed input is a 422."""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return UUID(data[_ID_KEY])
    except (ValueError, KeyError, TypeError) as exc:
        # binascii.Error (bad base64) and JSONDecodeError subclass ValueError.
        raise ValidationError("Invalid cursor", details={"cursor": cursor}) from exc
