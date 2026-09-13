"""agent operations and document revisions

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-14

Controlled agent document drafting and persistence (task 09-13):

- `agent_operations`: one structured draft/patch against a target document,
  with the base version it was drafted from (optimistic concurrency anchor),
  an explicit lifecycle state (running/completed/interrupted/failed/applied),
  an optional caller-supplied idempotency key (partial unique index), and
  JSONB draft/result/error payloads. Drafts and interrupted runs live ONLY
  here — never in chat_messages.
- `document_revisions`: one immutable content snapshot per successful apply
  (idempotency guarantees a single revision per operation).

Rollback path: `alembic downgrade 0008` drops `document_revisions` first
(FK target of nothing, but its own FK references `agent_operations`), then
`agent_operations`, then the `operation_state` enum type — the full reverse
of this revision; dropping these tables discards drafts and the revision
audit trail but never touches `documents` or `chat_messages` rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Explicit enum lifecycle (database-guidelines): op.create_table's implicit
    # CREATE TYPE is not checkfirst-guarded, which breaks upgrade after a
    # downgrade left the type behind; create_type=False on the column avoids
    # the double create.
    sa.Enum(
        "running", "completed", "interrupted", "failed", "applied", name="operation_state"
    ).create(op.get_bind(), checkfirst=True)
    op.create_table(
        "agent_operations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("base_document_version", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state",
            # PG-specific ENUM: the generic sa.Enum silently ignores
            # create_type=False, which re-emits CREATE TYPE and collides with
            # the explicit one above.
            postgresql.ENUM(
                "running",
                "completed",
                "interrupted",
                "failed",
                "applied",
                name="operation_state",
                create_type=False,
            ),
            server_default="running",
            nullable=False,
        ),
        sa.Column("draft", postgresql.JSONB(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_agent_operations_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_operations")),
    )
    # Idempotent create/apply: the caller's retry handle resolves to at most
    # one operation; NULL keys are exempt (partial index, mirrors the model).
    op.create_index(
        "ux_agent_operations_idempotency_key",
        "agent_operations",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_agent_operations_document_id",
        "agent_operations",
        ["document_id", "id"],
        unique=False,
    )
    op.create_table(
        "document_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_revisions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["agent_operations.id"],
            name=op.f("fk_document_revisions_operation_id_agent_operations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_revisions")),
    )
    op.create_index(
        "ix_document_revisions_document_id",
        "document_revisions",
        ["document_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    # Reversal order matters: revisions reference operations.
    op.drop_index("ix_document_revisions_document_id", table_name="document_revisions")
    op.drop_table("document_revisions")
    op.drop_index("ix_agent_operations_document_id", table_name="agent_operations")
    op.drop_index("ux_agent_operations_idempotency_key", table_name="agent_operations")
    op.drop_table("agent_operations")
    sa.Enum(name="operation_state").drop(op.get_bind(), checkfirst=True)
