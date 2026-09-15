"""resource tenant isolation: non-null tenant_id on documents, chat_sessions,
agent_operations, document_revisions

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-15

Stage 5 of the multi-tenant rollout (task 09-15-gateway-integration-v2):

- Add nullable `tenant_id` (UUID) to `documents`, `chat_sessions`,
  `agent_operations`, and `document_revisions`.
- Backfill every existing row with the deterministic default tenant
  inserted by migration 0010 (fixed UUID, idempotent re-insert here so the
  revision stays safe even if a squashed history re-runs it against a
  database whose `tenants` table lost the row).
- Tighten the columns to NOT NULL and add the foreign keys
  (`tenants.id`, ondelete CASCADE) plus the tenant-scoped lookup indexes
  mirroring the ORM models.

Rollback path: `alembic downgrade 0010` drops the four indexes, the four
foreign-key constraints, and the four columns — the exact reverse. No other
table is touched; resource rows themselves are preserved.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None

# Same deterministic default tenant as migration 0010 (and app.models.tenant).
_DEFAULT_TENANT_ID = "3d1f0a56-7c9e-5b41-9a2d-6f8e1c0b7a10"

# (table, tenant index name, extra index columns after tenant_id).
_TABLES: list[tuple[str, str, list[str]]] = [
    ("documents", "ix_documents_tenant_id", ["id"]),
    ("chat_sessions", "ix_chat_sessions_tenant_updated_id", ["updated_at", "id"]),
    ("agent_operations", "ix_agent_operations_tenant_id", ["id"]),
    ("document_revisions", "ix_document_revisions_tenant_id", ["id"]),
]


def upgrade() -> None:
    # Idempotent safety net: the default tenant must exist before the backfill.
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, slug, name, status) "
            "VALUES (:id, 'default', 'Default tenant', 'active') "
            "ON CONFLICT (slug) DO NOTHING"
        ).bindparams(sa.bindparam("id", _DEFAULT_TENANT_ID, type_=sa.UUID()))
    )

    for table, _index, _extra in _TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.UUID(), nullable=True))
        op.execute(
            sa.text(f"UPDATE {table} SET tenant_id = :tenant WHERE tenant_id IS NULL").bindparams(
                sa.bindparam("tenant", _DEFAULT_TENANT_ID, type_=sa.UUID())
            )
        )
        op.alter_column(table, "tenant_id", nullable=False)

    for table, _index, _extra in _TABLES:
        op.create_foreign_key(
            f"fk_{table}_tenant_id_tenants",
            table,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id", "id"], unique=False)
    op.create_index(
        "ix_chat_sessions_tenant_updated_id",
        "chat_sessions",
        ["tenant_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_agent_operations_tenant_id",
        "agent_operations",
        ["tenant_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_document_revisions_tenant_id",
        "document_revisions",
        ["tenant_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_revisions_tenant_id", table_name="document_revisions")
    op.drop_index("ix_agent_operations_tenant_id", table_name="agent_operations")
    op.drop_index("ix_chat_sessions_tenant_updated_id", table_name="chat_sessions")
    op.drop_index("ix_documents_tenant_id", table_name="documents")

    for table, _index, _extra in _TABLES:
        op.drop_constraint(f"fk_{table}_tenant_id_tenants", table, type_="foreignkey")
        op.drop_column(table, "tenant_id")
