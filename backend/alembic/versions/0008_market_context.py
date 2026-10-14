"""Store external derivatives market context.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_context",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source", "metric", "symbol", "ts"),
    )
    op.create_index("ix_market_context_lookup", "market_context", ["metric", "symbol", "ts"])


def downgrade() -> None:
    op.drop_index("ix_market_context_lookup", table_name="market_context")
    op.drop_table("market_context")
