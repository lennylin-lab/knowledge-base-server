"""Migration tests for the tenant identity schema (Stage 4).

Runs the real Alembic revisions against a disposable database
(`kb_migrate_test`, recreated from scratch every session — a stale schema
cannot poison the run). Verifies the upgrade → downgrade → upgrade round
trip (the enum-lifecycle trap), the deterministic default-tenant backfill,
and constraint presence.

`db`-marked: auto-skipped when PostgreSQL is unreachable (offline gate).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATE_DB = os.environ.get(
    "KB_TEST_MIGRATION_URL", "postgresql+asyncpg://kb:kb@localhost:5432/kb_migrate_test"
)
# The deterministic backfill constant from migration 0010.
DEFAULT_TENANT_ID = "3d1f0a56-7c9e-5b41-9a2d-6f8e1c0b7a10"
IDENTITY_TABLES = {"tenants", "users", "tenant_memberships"}
IDENTITY_ENUMS = {
    "tenant_status",
    "user_status",
    "membership_role",
    "membership_status",
}


def _maintenance_engine() -> AsyncEngine:
    from sqlalchemy.engine import make_url

    url = make_url(MIGRATE_DB).set(database="postgres")
    return create_async_engine(
        url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )


async def _recreate_database() -> None:
    """Drop and recreate the migration database: migrations must always run
    against fresh state, never a stale partially-migrated schema."""
    database = sqlalchemy.engine.make_url(MIGRATE_DB).database
    maintenance = _maintenance_engine()
    try:
        async with maintenance.connect() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database},
            )
            if exists.scalar_one_or_none() is not None:
                # WITH (FORCE) terminates stale connections first (PG13+);
                # identifiers cannot be bound parameters, and the name comes
                # from trusted test config.
                await conn.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        await maintenance.dispose()


@pytest.fixture(scope="module")
def alembic_cfg() -> AsyncIterator[Config]:
    """Alembic config pointed at the disposable database via the Settings
    env (alembic/env.py reads `get_settings().DATABASE_URL`)."""
    old_url = os.environ.get("KB_DATABASE_URL")
    os.environ["KB_DATABASE_URL"] = MIGRATE_DB
    from app.core.config import get_settings

    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    yield cfg
    if old_url is None:
        os.environ.pop("KB_DATABASE_URL", None)
    else:
        os.environ["KB_DATABASE_URL"] = old_url
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def migrated_url(alembic_cfg: Config) -> Iterator[str]:
    """Recreate the disposable DB and migrate it to head (module scope).

    Sync fixture on purpose: module-scoped async fixtures would outlive the
    function-scoped event loop (see conftest's `test_database_url`)."""
    asyncio.run(_recreate_database())
    command.upgrade(alembic_cfg, "head")
    yield MIGRATE_DB


@pytest.fixture
async def migrated_engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    """Per-test engine on the migrated database (fresh event loop)."""
    engine = create_async_engine(migrated_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {row[0] for row in rows}


async def _enum_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))
        return {row[0] for row in rows}


async def test_upgrade_creates_identity_tables_and_enums(migrated_engine: AsyncEngine) -> None:
    assert await _table_names(migrated_engine) >= IDENTITY_TABLES
    assert await _enum_names(migrated_engine) >= IDENTITY_ENUMS


async def test_default_tenant_backfill_is_deterministic(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id, slug, status FROM tenants WHERE slug = 'default'"))
        ).fetchall()
    assert len(rows) == 1
    assert str(rows[0][0]) == DEFAULT_TENANT_ID
    assert rows[0][2] == "active"


async def test_full_downgrade_reverses_upgrade(
    migrated_engine: AsyncEngine, alembic_cfg: Config
) -> None:
    """Downgrade to 0009 removes tables AND enum types (the enum-lifecycle
    trap), and a fresh upgrade re-creates everything with the same backfill."""
    # env.py drives migrations via asyncio.run — must leave this test's
    # running loop, so the (sync) alembic command runs in a worker thread.
    await asyncio.to_thread(command.downgrade, alembic_cfg, "0009")
    assert not (IDENTITY_TABLES & await _table_names(migrated_engine))
    assert not (IDENTITY_ENUMS & await _enum_names(migrated_engine))

    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    assert await _table_names(migrated_engine) >= IDENTITY_TABLES
    assert await _enum_names(migrated_engine) >= IDENTITY_ENUMS
    # Backfill re-runs deterministically: still exactly one default tenant.
    async with migrated_engine.connect() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM tenants WHERE slug = 'default'"))
        ).scalar_one()
    assert count == 1


UID1 = "11111111-1111-5111-8111-111111111111"
UID2 = "11111111-1111-5111-8111-222222222222"


async def test_resource_tenant_columns_non_null_with_fk(migrated_engine: AsyncEngine) -> None:
    """Stage 5 (0011): the four resource tables carry a non-null tenant_id FK;
    the constraint is enforced (a document without a tenant is rejected) and
    an unknown tenant id is rejected by the foreign key."""
    async with migrated_engine.connect() as conn:
        nullables = await conn.execute(
            text(
                "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
                "WHERE column_name = 'tenant_id' AND table_name IN "
                "('documents', 'chat_sessions', 'agent_operations', 'document_revisions')"
            )
        )
        assert all(row[2] == "NO" for row in nullables.fetchall())
        fks = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE contype = 'f' "
                "AND conname LIKE 'fk_%_tenant_id_tenants'"
            )
        )
        assert {row[0] for row in fks.fetchall()} >= {
            f"fk_{table}_tenant_id_tenants"
            for table in (
                "documents",
                "chat_sessions",
                "agent_operations",
                "document_revisions",
            )
        }

    doc_id = "33333333-3333-5333-8333-111111111111"
    async with migrated_engine.begin() as conn:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO documents (id, title, content) "
                    "VALUES (:did, 't', 'c')"  # tenant_id omitted -> NOT NULL violation
                ),
                {"did": doc_id},
            )
    async with migrated_engine.begin() as conn:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, title, content) "
                    "VALUES (:did, :tid, 't', 'c')"
                ),
                {"did": doc_id, "tid": "44444444-4444-5444-8444-111111111111"},
            )


async def test_resource_isolation_downgrade_to_0010_round_trips(
    migrated_engine: AsyncEngine, alembic_cfg: Config
) -> None:
    """0011's own downgrade removes the tenant columns (and nothing else) and
    a fresh upgrade restores them with the default-tenant backfill intact."""
    await asyncio.to_thread(command.downgrade, alembic_cfg, "0010")
    async with migrated_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE column_name = 'tenant_id' AND table_name IN "
                "('documents', 'chat_sessions', 'agent_operations', 'document_revisions')"
            )
        )
        assert rows.scalar_one() == 0
    # Resource tables themselves survive the downgrade (never drop rows).
    assert {"documents", "chat_sessions", "agent_operations", "document_revisions"} <= (
        await _table_names(migrated_engine)
    )

    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    async with migrated_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE column_name = 'tenant_id' AND is_nullable = 'NO' AND table_name IN "
                "('documents', 'chat_sessions', 'agent_operations', 'document_revisions')"
            )
        )
        assert rows.scalar_one() == 4


async def test_membership_constraints_enforced(migrated_engine: AsyncEngine) -> None:
    """Role is a closed set; (tenant, user) and subject are unique.

    Each violation gets its own transaction: once PG aborts a transaction,
    every later statement in it fails until rollback."""
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, subject) VALUES (:uid, 'sub-mig-test')"),
            {"uid": UID1},
        )
        await conn.execute(
            text("INSERT INTO users (id, subject) VALUES (:uid, 'sub-mig-test-2')"),
            {"uid": UID2},
        )
        tenant_id = await conn.scalar(text("SELECT id FROM tenants WHERE slug = 'default'"))
        await conn.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:mid, :tid, :uid, 'editor')"
            ),
            {"mid": "22222222-2222-5222-8222-111111111111", "tid": tenant_id, "uid": UID1},
        )

    # Duplicate (tenant, user) rejected.
    async with migrated_engine.begin() as conn:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                    "VALUES (:mid, :tid, :uid, 'editor')"
                ),
                {"mid": "22222222-2222-5222-8222-222222222222", "tid": tenant_id, "uid": UID1},
            )
    # Unknown role rejected by the enum type (asyncpg surfaces this as a
    # generic DBAPIError, not IntegrityError).
    async with migrated_engine.begin() as conn:
        with pytest.raises(sqlalchemy.exc.DBAPIError):
            await conn.execute(
                text(
                    "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                    "VALUES (:mid, :tid, :uid, 'superadmin')"
                ),
                {"mid": "22222222-2222-5222-8222-333333333333", "tid": tenant_id, "uid": UID2},
            )
    # Duplicate subject rejected.
    async with migrated_engine.begin() as conn:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await conn.execute(
                text("INSERT INTO users (id, subject) VALUES (:uid, 'sub-mig-test')"),
                {"uid": "11111111-1111-5111-8111-333333333333"},
            )
