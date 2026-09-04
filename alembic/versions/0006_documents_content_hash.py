"""add documents.content_hash

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-05

SHA-256 hex digest of `content` (front matter included); the service layer
compares it to skip reindexing byte-identical saves. Existing rows are
backfilled so already-indexed documents do not trigger one spurious reindex
on their next identical-content save.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # pgcrypto is available in the pgvector/pgvector:pg16 image and is used
    # only for this one-off backfill; IF NOT EXISTS keeps shared clusters safe.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.add_column("documents", sa.Column("content_hash", sa.Text(), nullable=True))
    # Hash exactly what is stored (front matter included), so a re-save of
    # unchanged content hashes equal and takes the skip path.
    op.execute("UPDATE documents SET content_hash = encode(digest(content, 'sha256'), 'hex')")


def downgrade() -> None:
    # pgcrypto is deliberately NOT dropped: other users/objects may depend on
    # it, and dropping an extension is a cluster-level decision.
    op.drop_column("documents", "content_hash")
