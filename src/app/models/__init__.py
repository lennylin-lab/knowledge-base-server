"""ORM models. Importing this package registers every table on Base.metadata
(what Alembic autogen compares against)."""

from __future__ import annotations

from app.models.document import Document, IndexStatus

__all__ = ["Document", "IndexStatus"]
