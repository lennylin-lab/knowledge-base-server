"""chat_sessions rolling summary columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-11

Per-session rolling summary of turns evicted from the history token window:
`rolling_summary` holds the compressed text, `summarized_through_id` is the
incremental watermark (id of the newest message already folded in). Both
nullable, deliberately no backfill — existing sessions simply have no summary
until new turns evict (the summary is derived state; messages stay whole).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("rolling_summary", sa.Text(), nullable=True))
    op.add_column("chat_sessions", sa.Column("summarized_through_id", sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "summarized_through_id")
    op.drop_column("chat_sessions", "rolling_summary")
