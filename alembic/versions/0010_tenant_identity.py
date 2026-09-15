"""tenant identity tables and default-tenant backfill

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-15

Tenant identity schema (task 09-15-gateway-integration-v2, Stage 4):

- `tenants`: stable slug (natural key), name, constrained status.
- `users`: immutable OIDC `sub` as THE identity key (`ux_users_subject`);
  email/display_name are display data only.
- `tenant_memberships`: one role+status per (tenant, user), unique pair,
  user-id index for login-time membership resolution.

Backfill: exactly one deterministic default tenant (fixed UUID
`3d1f0a56-7c9e-5b41-9a2d-6f8e1c0b7a10`, slug "default") inserted
idempotently via ON CONFLICT DO NOTHING, so re-running the revision (or a
later tightening migration re-using the helper) is safe. Existing resource
rows get their `tenant_id` in the Stage 5 migration by joining this slug.

Rollback path: `alembic downgrade 0009` drops `tenant_memberships`, then
`users`, then `tenants`, then the four enum types — the exact reverse of
the upgrade. Nothing outside the three new tables is touched, so no
existing rows are lost. The default tenant's fixed UUID is deterministic
on re-upgrade; any future downgrade+upgrade cycle re-inserts it with the
same id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None

# Deterministic default-tenant identity: uuid5(NAMESPACE_URL, "kb:tenant:default").
DEFAULT_TENANT_ID = "3d1f0a56-7c9e-5b41-9a2d-6f8e1c0b7a10"
DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_NAME = "Default tenant"


def _create_enum(name: str, *values: str) -> None:
    # Explicit enum lifecycle (database-guidelines.md): op.create_table's
    # implicit CREATE TYPE is not checkfirst-guarded, which breaks the
    # upgrade -> downgrade -> upgrade round trip.
    sa.Enum(*values, name=name).create(op.get_bind(), checkfirst=True)


def _drop_enum(name: str) -> None:
    sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)


def upgrade() -> None:
    _create_enum("tenant_status", "active", "suspended")
    _create_enum("user_status", "active", "disabled")
    _create_enum("membership_role", "tenant_admin", "editor", "member", "viewer", "service_account")
    _create_enum("membership_status", "active", "disabled")

    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("active", "suspended", name="tenant_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name="ux_tenants_slug"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("active", "disabled", name="user_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("subject", name="ux_users_subject"),
    )

    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "tenant_admin",
                "editor",
                "member",
                "viewer",
                "service_account",
                name="membership_role",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM("active", "disabled", name="membership_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
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
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_memberships_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tenant_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_memberships")),
        sa.UniqueConstraint("tenant_id", "user_id", name="ux_tenant_memberships_tenant_user"),
    )
    # Login-time membership resolution ("which tenants may this subject enter?").
    op.create_index(
        "ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"], unique=False
    )

    # Deterministic default tenant: idempotent insert (safe on re-run and
    # under the unique slug constraint), fixed UUID so later tightening
    # migrations can backfill resource rows by a stable constant.
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, slug, name, status) "
            "VALUES (:id, :slug, :name, 'active') "
            "ON CONFLICT (slug) DO NOTHING"
        ).bindparams(
            sa.bindparam("id", DEFAULT_TENANT_ID, type_=sa.UUID()),
            sa.bindparam("slug", DEFAULT_TENANT_SLUG),
            sa.bindparam("name", DEFAULT_TENANT_NAME),
        )
    )


def downgrade() -> None:
    # Reversal order: memberships reference both other tables.
    op.drop_index("ix_tenant_memberships_user_id", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")
    op.drop_table("users")
    op.drop_table("tenants")
    _drop_enum("membership_status")
    _drop_enum("membership_role")
    _drop_enum("user_status")
    _drop_enum("tenant_status")
