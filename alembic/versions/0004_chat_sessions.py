"""chat sessions and messages

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-02

Multi-turn chat persistence: soft-deletable sessions (keyset cursor index on
updated_at DESC, id DESC) and their messages (role enum, CASCADE on session
delete, chronological read index).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Explicit enum lifecycle (database-guidelines): op.create_table's implicit
    # CREATE TYPE is not checkfirst-guarded, which breaks upgrade after a
    # downgrade left the type behind; create_type=False on the column avoids
    # the double create.
    sa.Enum("user", "assistant", name="chat_message_role").create(op.get_bind(), checkfirst=True)
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_sessions")),
    )
    # Keyset pagination cursor support: ORDER BY updated_at DESC, id DESC.
    op.create_index(
        "ix_chat_sessions_updated_at_id",
        "chat_sessions",
        [sa.literal_column("updated_at DESC, id DESC")],
        unique=False,
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column(
            "role",
            # PG-specific ENUM: the generic sa.Enum silently ignores
            # create_type=False, which re-emits CREATE TYPE and collides with
            # the explicit one above.
            postgresql.ENUM("user", "assistant", name="chat_message_role", create_type=False),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name=op.f("fk_chat_messages_session_id_chat_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
    )
    # History/detail reads: one session's messages, chronological.
    op.create_index(
        "ix_chat_messages_session_created",
        "chat_messages",
        ["session_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_created", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_sessions_updated_at_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    # Reverse the explicit type create so the downgrade fully restores the
    # pre-revision catalog (drop_table does not drop a type it did not make).
    sa.Enum(name="chat_message_role").drop(op.get_bind(), checkfirst=True)
