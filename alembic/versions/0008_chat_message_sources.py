"""chat_messages sources column

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-11

Per-assistant-message retrieval sources: `sources` holds the run's full
SearchHit list (JSON array, retrieval order) so a follow-up turn can re-emit
and re-cite the previous run's numbered blocks. Nullable, deliberately no
backfill — user messages and failed runs stay NULL, and pre-feature assistant
rows simply carry nothing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("sources", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "sources")
