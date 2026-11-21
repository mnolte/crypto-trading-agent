"""Paper fills and the meta table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("qty", sa.String(), nullable=False),
        sa.Column("reference_price", sa.String(), nullable=True),
        sa.Column("fill_price", sa.String(), nullable=False),
        sa.Column("order_type", sa.String(), nullable=False),
        sa.Column("fee_eur", sa.String(), nullable=False),
        sa.Column("fee_kind", sa.String(), nullable=False),
        sa.Column("slippage_eur", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("paper_order_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
    )
    op.create_index("ix_fills_account_at", "fills", ["account_id", "at"])
    op.create_index("ix_fills_account_pair_at", "fills", ["account_id", "pair", "at"])
    op.create_table(
        "meta",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("meta")
    op.drop_index("ix_fills_account_pair_at", table_name="fills")
    op.drop_index("ix_fills_account_at", table_name="fills")
    op.drop_table("fills")
