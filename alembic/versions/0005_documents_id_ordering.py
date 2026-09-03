"""drop documents (created_at, id) cursor index

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-04

Entity PKs are UUIDv7 now: id order is creation order, so the documents
keyset listing orders by id alone and the primary-key index serves the
sort. The (created_at DESC, id DESC) composite is dead weight.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index("ix_documents_created_at_id", table_name="documents")


def downgrade() -> None:
    op.create_index(
        "ix_documents_created_at_id",
        "documents",
        [sa.literal_column("created_at DESC, id DESC")],
        unique=False,
    )
