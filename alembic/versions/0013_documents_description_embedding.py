"""documents description_embedding column

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-25

Document-level embedding of the front-matter `description` blurb (the
association service's third recall leg). Nullable: rows without a
description — or not yet re-indexed since this change — read "no
description vector". The indexing pipeline populates (or clears) it on
every run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# Pinned literal, not `EMBEDDING_DIM` from app code: a migration is a historical
# schema record — if the constant ever moves, a fresh `upgrade head` must still
# create this column at the dimension it shipped with (migration 0003 set the
# precedent).
DIMENSION = 1536

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("description_embedding", Vector(DIMENSION), nullable=True),
    )
    op.create_index(
        "ix_documents_description_embedding_hnsw",
        "documents",
        ["description_embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"description_embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_documents_description_embedding_hnsw", table_name="documents")
    op.drop_column("documents", "description_embedding")
