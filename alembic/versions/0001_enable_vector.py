"""enable vector extension

Revision ID: 0001
Revises:
Create Date: 2026-08-27

Enables pgvector — the extension backing every embedding column. The compose
PostgreSQL image (pgvector/pgvector:pg16) ships the extension binaries.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # Deliberately without CASCADE: if vector-typed data exists, dropping the
    # extension would destroy it, so fail loudly instead.
    op.execute("DROP EXTENSION IF EXISTS vector")
