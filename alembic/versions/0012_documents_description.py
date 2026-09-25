"""documents description column

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-24

Front-matter-derived short summary shown in the document header (below tags).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("description", sa.Text(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("documents", "description")
