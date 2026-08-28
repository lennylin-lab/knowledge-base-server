"""documents table

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-28

Core knowledge-base entity: markdown documents with front-matter-derived
title/tags columns, soft delete, and the reserved owner_id column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Explicit enum lifecycle: op.create_table's implicit CREATE TYPE is not
    # checkfirst-guarded, which breaks upgrade after a downgrade left the
    # type behind. create_type=False on the column avoids the double create.
    sa.Enum("pending", "done", "failed", name="index_status").create(op.get_bind(), checkfirst=True)
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "index_status",
            # PG-specific ENUM: the generic sa.Enum silently ignores
            # create_type=False ("backend-inapplicable kwargs are ignored"),
            # which re-emits CREATE TYPE and collides with the explicit one.
            postgresql.ENUM("pending", "done", "failed", name="index_status", create_type=False),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("owner_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    # Keyset pagination cursor support: ORDER BY created_at DESC, id DESC.
    op.create_index(
        "ix_documents_created_at_id",
        "documents",
        [sa.literal_column("created_at DESC, id DESC")],
        unique=False,
    )
    # Tag membership filter (?tag=).
    op.create_index(
        "ix_documents_tags", "documents", ["tags"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_documents_tags", table_name="documents", postgresql_using="gin")
    op.drop_index("ix_documents_created_at_id", table_name="documents")
    op.drop_table("documents")
    # The table create emits CREATE TYPE implicitly (checkfirst); reverse it
    # explicitly so the downgrade fully restores the pre-revision catalog.
    sa.Enum(name="index_status").drop(op.get_bind(), checkfirst=True)
