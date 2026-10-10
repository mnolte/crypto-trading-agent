"""Market data: candles and markets tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candles",
        sa.Column("market", sa.String(), primary_key=True),
        sa.Column("tf", sa.String(), primary_key=True),
        sa.Column("open_time_ms", sa.Integer(), primary_key=True),
        sa.Column("open", sa.String(), nullable=False),
        sa.Column("high", sa.String(), nullable=False),
        sa.Column("low", sa.String(), nullable=False),
        sa.Column("close", sa.String(), nullable=False),
        sa.Column("volume", sa.String(), nullable=False),
    )
    op.create_table(
        "markets",
        sa.Column("market", sa.String(), primary_key=True),
        sa.Column("base", sa.String(), nullable=False, server_default=""),
        sa.Column("quote", sa.String(), nullable=False, server_default="EUR"),
        sa.Column("status", sa.String(), nullable=False, server_default=""),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("failed_rule", sa.String(), nullable=True),
        sa.Column("volume_30d_eur", sa.Float(), nullable=True),
        sa.Column("spread_pct", sa.Float(), nullable=True),
        sa.Column("daily_candles", sa.Integer(), nullable=True),
        sa.Column("below_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("within_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("markets")
    op.drop_table("candles")
